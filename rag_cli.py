from time import time
start = time()
import logging
from itertools import batched
from pathlib import PosixPath
from textwrap import dedent
from collections import namedtuple
from tomllib import load
from csv import DictReader, Error as CSVError
from sys import stdout

from chonkie import SemanticChunker
from configargparse import ArgParser, Namespace, FileType
from elasticsearch.dsl import connections, Q

from adapters import STAPIEmbeddings, LlamacppAPI
from esdocs import Chapter, Chunk, Story

from typing import Type, Union
from elasticsearch.dsl import Document
from elasticsearch import NotFoundError

ChapterOffset = namedtuple("ChapterOffset", ["id", "number", "start", "end"])


def load_config() -> Namespace:
	config_path = PosixPath(__file__).parent / "index-fics.ini"
	enrich_config = ArgParser(default_config_files=[str(config_path)], ignore_unknown_config_file_keys=True)
	enrich_config.add_argument('-c', '--config', is_config_file=True, help='config file path')
	api_auth_config = enrich_config.add_argument_group(title="API authentication (will be preferred if both are set)")
	basic_auth_config = enrich_config.add_argument_group(title="Basic authentication")
	api_auth_config.add_argument("--api-id")
	api_auth_config.add_argument("--api-secret")
	basic_auth_config.add_argument("--username")
	basic_auth_config.add_argument("--password")
	enrich_config.add_argument("--es-ca-cert-path", required=True)
	enrich_config.add_argument("--es-hosts", action="append", required=True)
	enrich_config.add_argument("--vaguesearch", type=FileType("rb"), required=True)
	args = enrich_config.parse_args()
	vaguesearch = load(args.vaguesearch)
	args.vaguesearch = vaguesearch
	return args

def setup_logging():
	vaguelogger = logging.getLogger("vaguelogger")
	vaguelogger.setLevel(logging.INFO)
	stdout_stream = logging.StreamHandler(stdout)
	print_formatter = logging.Formatter("%(message)s")
	stdout_stream.setFormatter(print_formatter)
	vaguelogger.addHandler(stdout_stream)
	#logging.basicConfig(format="%(message)s", level=logging.INFO, handlers=[stdout_stream])


def setup_elasticsearch(configuration):
	if configuration.api_id:
		authentication = {
			"api_key": (configuration.api_id, configuration.api_secret)
		}
	else:
		authentication = {
			"basic_auth": (configuration.username, configuration.password)
		}
	# https://elasticsearch-py.readthedocs.io/en/stable/api/elasticsearch.html#elasticsearch
	conn = connections.create_connection(hosts=configuration.es_hosts,
											ca_certs=configuration.es_ca_cert_path,
											request_timeout=600, # high timeout is critical for bulk indexing!
											**authentication)
	es_transport_logger = logging.getLogger('elastic_transport.transport')
	es_transport_logger.setLevel(logging.WARNING) # don't log every single request to ES...
	traffic_logger = logging.getLogger("urllib3")
	traffic_logger.setLevel(logging.WARNING) # debug level will log the whole request body...

	store_composable_template(Chunk)


def store_composable_template(doc_class: Type[Document]):
	conn = connections.get_connection()
	nodes = conn.nodes.info()["_nodes"]["total"]
	legacy_index_template = doc_class._index.as_template("ignore").to_dict()
	index_wild = legacy_index_template["index_patterns"][0]
	index_prefix = index_wild[:-2]
	del(legacy_index_template["index_patterns"])
	legacy_index_template["settings"]["number_of_shards"] = nodes
	legacy_index_template["mappings"]["dynamic"] = "strict"
	template_name = f"elasticfics-{index_prefix}"
	try:
		found_tmpl =conn.indices.get_index_template(name=template_name)
		found_tmpl = found_tmpl.body["index_templates"][0]["index_template"]["template"]
		new_settings = {}
		for setting in found_tmpl["settings"]["index"]:
			if found_tmpl["settings"]["index"][setting].isdigit():
				new_settings[setting] = int(found_tmpl["settings"]["index"][setting])
			else:
				new_settings[setting] = found_tmpl["settings"]["index"][setting]
	except (NotFoundError, KeyError, IndexError):
		vaguelogger.info(f"Saving index template for {index_wild} with {nodes} shards")
		conn.indices.put_index_template(name=template_name, template=legacy_index_template, index_patterns=[index_wild])
		return
	update = False
	if legacy_index_template["mappings"] != found_tmpl["mappings"]:
		update = True
	if legacy_index_template["settings"] != new_settings:
		update = True
	if update:
		vaguelogger.info(f"Saving index template for {index_wild} with {nodes} shards")
		conn.indices.put_index_template(name=template_name, template=legacy_index_template, index_patterns=[index_wild])


def load_story(story_id: int) -> tuple[str, list[ChapterOffset]]:
	search = Chapter.search()
	search = search.params(source=["chapter.id", "chapter.number", "chapter.text"])
	filter_ = Q("term", story__id=story_id)
	filter_ = filter_ & Q("range", chapter__number={"gte": 0})
	search = search.filter(filter_)
	search = search.sort("chapter.number")
	search = search.extra(size=200) # TODO: check for ghosts
	resp = search.execute()
	text = ""
	offsets: list[ChapterOffset] = []
	for hit in resp.hits:
		block_size = len(text)
		offsets.append(ChapterOffset(hit.chapter.id, hit.chapter.number, block_size, block_size + len(hit.chapter.text)))
		text += hit.chapter.text
	return text, offsets


def reconstruct_chunks(chunks: list[Chunk], context: bool = False):
	story_id = chunks[0].story.id
	story_search = Story.search()
	story_search = story_search.filter(Q("term", id=story_id))
	story_search = story_search.extra(source=["deleted"], size=1)
	story_results = story_search.execute()
	chapter2get = set()
	for chunk in chunks:
		chapter2get.update(chunk.chapter.number)
	chapter_matches = [
		Q("term", chapter__number=chapter)
		for chapter in chapter2get
	]
	story_filter = Q("term", story__id=story_id)
	chapter_filter = Q("bool", must=story_filter, should=chapter_matches, minimum_should_match=1)
	search = Chapter.search()
	search = search.params(source=["chapter.number", "chapter.text", "chapter.id"], size=len(chapter2get))
	search = search.filter(chapter_filter)
	resp = search.execute()
	if resp.hits.total.value != len(chapter2get):
		vaguelogger.warning(f"the wrong number of chapters are in ES for {search.to_dict()}")
	chapter_texts = {
		hit.chapter.number: hit.chapter.text
		for hit in resp.hits
	}

	for chunk in chunks:
		segment = ""
		for i, chapter in enumerate(chunk.chapter.number):
			slice_start = chunk.chapter.start[i]
			slice_end = chunk.chapter.end[i]
			segment += chapter_texts[chapter][slice_start:slice_end]
			if not hasattr(chunk, "pcent"):
				chunk.pcent = f"{len(segment) / len(chapter_texts[chapter]):.0%}"
		if story_results.hits[0].deleted:
			chunk.link = f"https://fimfetch.net/story/{story_id}/a/{chunk.chapter.number[0]}"
		else:
			chunk.link = f"https://www.fimfiction.net/story/{story_id}/{chunk.chapter.id[0]}"
		chunk.text = segment
	return chunks


def embed_story(embedder: STAPIEmbeddings, story_id: int):
	start = time()
	if Chunk.is_embedded(story_id):
		return
	chunker = SemanticChunker(embedding_model=embedder, chunk_size=512, skip_window=2)
	story, offsets = load_story(story_id)
	step = time()
	vaguelogger.info(f"{step - start:.2f}s loading story")
	start = step
	chunked = chunker.chunk(story)
	step = time()
	vaguelogger.info(f"{step - start:.2f}s chunking")
	start = step
	for i, chunk in enumerate(chunked):
		es_chunk = Chunk(order=i)
		es_chunk.story.id = story_id
		involved_filter = lambda chapter: chunk.start_index < chapter.end and chunk.end_index > chapter.start
		involved_chapters = list(filter(involved_filter, offsets))
		es_chunk.chapter.number = []
		es_chunk.chapter.id = []
		es_chunk.chapter.start = []
		es_chunk.chapter.end = []
		for chapter in involved_chapters:
			es_chunk.chapter.number.append(chapter.number)
			es_chunk.chapter.id.append(chapter.id)
			text_start = chunk.start_index - chapter.start
			es_chunk.chapter.start.append(max(0, text_start))
			es_chunk.chapter.end.append(min(chapter.end, chunk.end_index) - chapter.start)


		#es_chunk.save(index=f"<{es_chunk._index._name[:-1]}" + "{now/d}>")
		chunk.doc = es_chunk
	for batch in batched(chunked, 10):
		texts = [chunk.text for chunk in batch]
		for chunk, embedding in zip(batch, embedder.final_embed(texts)):
			chunk.doc.embeddings = embedding
			chunk.doc.save(index=f"<{chunk.doc._index._name[:-1]}" + "{now/d}>")
	step = time()
	vaguelogger.info(f"{step - start:.2f}s embedding")


def wring_rag(story_id: int, params: dict, embedder: STAPIEmbeddings, chatter: LlamacppAPI):
	embed_story(embedder, story_id)
	prompt = params["llms"]["embedding"]["prompt"]["s2p"]
	prompt = dedent(prompt)
	prompt = prompt.strip() + " "
	relevance_query = params["story"]["one"]["relevance question"]
	relevance_query = dedent(relevance_query)
	relevance_query = prompt + relevance_query.strip()
	start = time()
	relevance_vector = embedder.final_embed([relevance_query])[0]
	Chunk._index.refresh()
	try:
		chunks = Chunk.find_related(story_id, relevance_vector)
		step = time()
		vaguelogger.info(f"{step - start:.2f}s retrieving")
		start = time()
	except ValueError:
		vaguelogger.warning(f"No semantically related chunks found for {story_id}.")
		return
	system_prompt = dedent(params["llms"]["chat"]["system prompt"]).strip()
	user_question = dedent(params["story"]["one"]["analysis question"]).strip()
	if user_question == "relevance question":
		user_question = dedent(params["story"]["one"]["relevance question"]).strip()

	rag_chunks = ""
	chat_tokens = chatter.count_tokens(system_prompt.format(chunks="")) + chatter.count_tokens(
		user_question) + 10
	max_prompt = chatter.context - params["llms"]["chat"]["min answer tokens"]
	for chunk in chunks:
		chat_tokens += chatter.count_tokens(chunk.as_blob())
		if chat_tokens > max_prompt:
			break
		chunk.to_chat = True
		rag_chunks += chunk.as_blob()

	system_prompt = system_prompt.format(chunks=rag_chunks)
	#chat_response = chatter.chat_response(system_prompt + user_question)
	chat_response = chatter.chat_response2(system_prompt, user_question)
	step = time()
	vaguelogger.info(f"{step - start:.2f}s asking")
	story_title = Story.get_title_lite(story_id) + ".txt"
	answer_path = PosixPath(params["story"]["many"]["log"])
	if not answer_path.is_absolute():
		prefix = PosixPath(__file__)
		answer_path = prefix / answer_path
	if not answer_path.is_dir():
		vaguelogger.error(f"The output path must exist: {answer_path}")
		exit(1)
	answer_path = answer_path / story_title.replace("/", "_")

	with answer_path.open("w") as fp:
		fp.write(user_question)
		fp.write("\n==================================================\n")
		fp.write(chat_response)
		fp.write("\n==================================================\n")
		for chunk in chunks:
			fp.write(chunk.as_blob(True))

def load_csv_maybe(csv_path: str, column: str) -> Union[list[int], bool]:
	csv_in_path = PosixPath(csv_path)
	if not csv_in_path.is_absolute():
		prefix = PosixPath(__file__)
		csv_in_path = prefix / csv_in_path
	if not csv_in_path.exists():
		return False
	try:
		with csv_in_path.open(newline="") as csv_in_fp:
			csv_in = DictReader(csv_in_fp)
			story_ids = [
				int(row[column])
				for row in csv_in
			]
			return story_ids
	except (KeyError, CSVError):
		return False

if __name__ == "__main__":
	step = time()
	setup_logging()
	vaguelogger = logging.getLogger("vaguelogger")
	vaguelogger.info(f"{step - start:.2f}s loading")
	my_config = load_config()
	setup_elasticsearch(my_config)

	embedder = STAPIEmbeddings(my_config.vaguesearch["llms"]["embedding"]["stapi host"])

	llama_cpp_client = LlamacppAPI(my_config.vaguesearch["llms"]["chat"]["host"])

	story_ids_maybe = load_csv_maybe(my_config.vaguesearch["story"]["many"]["list"],
									 my_config.vaguesearch["story"]["many"]["id column"])
	if story_ids_maybe:
		for story in story_ids_maybe:
			wring_rag(story, my_config.vaguesearch, embedder, llama_cpp_client)
		exit()
	embed_story(embedder, my_config.vaguesearch["story"]["one"]["id"])

	prompt = """
	Instruct: Given a web search query, retrieve relevant passages that answer the query.
	Query: {query}
	"""
	prompt = dedent(prompt).strip()
	prompt = prompt.format(query=dedent(my_config.vaguesearch["story"]["one"]["relevance question"]).strip())
	start = time()
	prompt_embed = embedder.final_embed([prompt])[0]
	Chunk._index.refresh()

	try:
		chunks = Chunk.find_related(my_config.vaguesearch["story"]["one"]["id"], prompt_embed)
	except ValueError:
		vaguelogger.error("No semantically related chunks found.")
		exit(1)
	step = time()
	vaguelogger.info(f"{step - start:.2f}s retrieving")
	start = step

	system_prompt = dedent(my_config.vaguesearch["llms"]["chat"]["system prompt"]).strip()
	user_question = dedent(my_config.vaguesearch["story"]["one"]["analysis question"]).strip()
	if user_question == "relevance question":
		user_question = dedent(my_config.vaguesearch["story"]["one"]["relevance question"]).strip()

	rag_chunks = ""
	chat_tokens = llama_cpp_client.count_tokens(system_prompt.format(chunks="")) + llama_cpp_client.count_tokens(user_question) + 10
	max_prompt = llama_cpp_client.context - my_config.vaguesearch["llms"]["chat"]["min answer tokens"]
	for chunk in chunks:
		chat_tokens += llama_cpp_client.count_tokens(chunk.as_blob())
		if chat_tokens > max_prompt:
			break
		chunk.to_chat = True
		rag_chunks += chunk.as_blob()

	system_prompt = system_prompt.format(chunks=rag_chunks)
	#chat_response = llama_cpp_client.chat_response(system_prompt + user_question)
	chat_response = llama_cpp_client.chat_response2(system_prompt, user_question)

	with open("ragout.txt", "w") as fp:
		fp.write(user_question)
		fp.write("\n==================================================\n")
		fp.write(chat_response)
		fp.write("\n==================================================\n")
		for chunk in chunks:
			fp.write(chunk.as_blob(True))
	step = time()
	vaguelogger.info(f"{step - start:.2f}s answering")
