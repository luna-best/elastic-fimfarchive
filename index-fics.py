import logging
from json import loads
from collections import namedtuple
from zipfile import ZipFile, ZIP_DEFLATED, BadZipfile, LargeZipFile
from re import compile
from io import TextIOWrapper
from threading import Thread, Event
from queue import Queue, Empty, Full
from signal import signal, SIGINT
from datetime import datetime
from pathlib import Path
from math import inf
from warnings import catch_warnings, simplefilter
from dataclasses import dataclass

from tqdm import tqdm
from configargparse import ArgParser, FileType
from elasticsearch_dsl import connections, Document
from elasticsearch.helpers import streaming_bulk, BulkIndexError
from elasticsearch.exceptions import ConnectionTimeout

from ebooklib.epub import EpubException, EpubReader
from ebooklib import ITEM_DOCUMENT
from collections.abc import Iterable
from typing import Type, ClassVar, NamedTuple, Union
from configargparse import Namespace
from re import Pattern

from esdocs import Chapter, Story
from folders import GroupMeta


class StoryFeed:
	zip_source: ZipFile
	def __init__(self, zip_source: ZipFile):
		self.zip_source = zip_source
		self.index_unparsed = TextIOWrapper(self.zip_source.open("index.json"), encoding="utf-8", newline="\n")

	def count_stories(self) -> int:
		print("Counting stories. Configure story-count to accelerate: ", end="")
		count = -2
		for line in self.index_unparsed.readlines():
			count += 1
		self.index_unparsed.close()
		self.index_unparsed = TextIOWrapper(self.zip_source.open("index.json"), encoding="utf-8", newline="\n")
		print(count)
		return count

	def stories(self) -> Iterable:
		# the index is almost ndjson, but somewhat unfortunately is valid json
		# some fuckery to transform the json into ndjson, then load it linewise
		# to avoid loading the whole 1GB file at once
		self.index_unparsed.readline()  # {
		almost_a_line = self.index_unparsed.readline()
		data_start = almost_a_line.find("{")
		while data_start > 0:
			if almost_a_line[-2] == ",":
				a_line = almost_a_line[data_start:-2]  # ,\n
			else:
				a_line = almost_a_line[data_start:-1]  # ,
			yield loads(a_line)
			almost_a_line = self.index_unparsed.readline()
			data_start = almost_a_line.find("{")
		return # }

@dataclass
class UnanalyzedStory:
	story_meta: dict
	epub_data: EpubReader
	archive_date: datetime
	group_db: Union[GroupMeta, bool]
	whitespace_pattern: ClassVar[Pattern] = compile(r"[\s]+")
	UnanalyzedChapter: ClassVar[NamedTuple] = namedtuple("UnanalyzedChapter", ["number", "title", "href"])

	def __post_init__(self):
		self.chapters_data = self.story_meta["chapters"]
		self.epub_path = self.story_meta["archive"]["path"]
		self.chapter_filename_pattern = compile(r"(Chapter(?P<simple_chapter_number>\d+)\.html)|"
										r"(Chapter(?P<split_chapter_number>\d+)_split_(?P<split_number>\d{3})\.html)")

	def analyze(self):
		# this section would be sped up by maintaining the chapter *file* order in the .epub in its output (never seek backwards)
		#first layer of merging. the epub chapter files may be
		# regular: 'Chapter1.html'
		# or split: 'Chapter19_split_000.html' , 'Chapter19_split_001.html'
		# generate a list by the # in Chapter# of either the file itself or a list of files
		unsplitted_toc = {}
		for chapter_file in self.epub_data.get_items_of_type(ITEM_DOCUMENT):
			chapter_match = self.chapter_filename_pattern.match(chapter_file.file_name)
			if chapter_match.groupdict()["simple_chapter_number"]:
				chapter_index = int(chapter_match.group("simple_chapter_number")) - 1
				unsplitted_toc[chapter_index] = chapter_file
			if chapter_match.groupdict()["split_chapter_number"]:
				chapter_index = int(chapter_match.group("split_chapter_number")) - 1
				if chapter_index not in unsplitted_toc.keys():
					unsplitted_toc[chapter_index] = [chapter_file]
				else:
					unsplitted_toc[chapter_index].append(chapter_file)

		#associate chapter files with the index.json list of chapters.
		# if the chapters don't match by number 1:1, then they are matched by comparing titles in the epub's toc.ncx
		# "ghost" (depublished, non-title-matching) chapters get their chapter number inverted
		chapter_map = []
		index = 0
		for epub_link in self.epub_data.toc:
			chapter_match = self.chapter_filename_pattern.match(epub_link.href)
			if chapter_match.groupdict()["simple_chapter_number"]:
				unsplitted_index = int(chapter_match.group("simple_chapter_number")) - 1
			else:
				unsplitted_index = int(chapter_match.group("split_chapter_number")) - 1
			epub_chapter = unsplitted_toc[unsplitted_index]
			#the most common case, no ghost chapters
			if len(self.chapters_data) == len(self.epub_data.toc):
				chapter_map.append(self.UnanalyzedChapter(index, epub_link.title, epub_chapter))
				index += 1
				continue

			#crashy ghost chapter properties
			if index > len(self.chapters_data) - 1 or self.chapters_data[index]["title"] is None:
				chapter_map.append(self.UnanalyzedChapter(unsplitted_index * -1, epub_link.title, epub_chapter))
				continue

			#replace whitespace characters and grouped whitespace character sequences with a single space
			normalized_title = self.whitespace_pattern.sub(" ", self.chapters_data[index]["title"])
			normalized_title = normalized_title.strip(" ") #leading and trailing whitespace
			if epub_link.title == normalized_title:
				chapter_map.append(self.UnanalyzedChapter(index, epub_link.title, epub_chapter))
				index += 1
			else:
				chapter_map.append(self.UnanalyzedChapter(unsplitted_index * -1, epub_link.title, epub_chapter))

		if self.group_db:
			groups_info = self.group_db.groups4story(self.story_meta["id"])
		else:
			groups_info = False

		for chapter in chapter_map:
			es_chapter = Chapter()
			es_chapter.analyze(chapter, self.story_meta, self.chapters_data, groups_info)
			yield es_chapter
		else:
			es_story = Story()
			es_story.analyze(es_chapter, self.story_meta, self.archive_date)
			yield es_story


class HackedEpubReader(EpubReader):
	def _load(self):
		try:
			self.zf = ZipFile(self.file_name, 'r', compression=ZIP_DEFLATED, allowZip64=True)
		except BadZipfile as bz:
			raise EpubException(0, 'Bad Zip file')
		except LargeZipFile as bz:
			raise EpubException(1, 'Large Zip file')

		# 1st check metadata
		self._load_container()
		self._load_opf_file()

		self.zf.close()


def read_epub(name, options=None) -> EpubReader:
	reader = HackedEpubReader(name, options)
	book = reader.load()
	reader.process()
	return book


def process_fics(configuration, es_queue: Queue, stop_event: Event):
	zip_file = ZipFile(configuration.fimfarchive)
	story_feed = StoryFeed(zip_file)
	print("Warnings will be logged to ./ingest.log.")
	logging.basicConfig(filename="ingest.log", format='%(asctime)s:[%(levelname)s] %(message)s', level=logging.INFO)
	story_file_pattern = compile(r".+/(?P<story_file>.+-\d+)")
	story_file_max_length = 20
	if configuration.story_count == 0:
		configuration.story_count = story_feed.count_stories()
		print(f"Set story-count = {configuration.story_count} for faster startup")
	first_checked = datetime.fromtimestamp(0)
	progress = tqdm(total=configuration.story_count, unit="story", smoothing=0.03)

	if "Advisory" in configuration.skip_tags:
		from advisory_skipper import generate_skips
		skips_generator = generate_skips()
		id_to_skip = next(skips_generator)
		configuration.skip_tags.remove("Advisory")
	else:
		id_to_skip = inf

	if configuration.folders_db:
		group_db = GroupMeta(configuration.folders_db)
	else:
		group_db = False

	for story_meta in story_feed.stories():
		if stop_event.is_set():
			progress.close()
			return

		if not first_checked.tzinfo:
			first_checked = datetime.fromisoformat(story_meta["archive"]["date_fetched"])

		if story_meta["id"] == id_to_skip:
			progress.update()
			continue
		if story_meta["id"] >= id_to_skip:
			try:
				id_to_skip = next(skips_generator)
			except StopIteration:
				id_to_skip = inf

		story_file = story_file_pattern.match(story_meta["archive"]["path"]).group("story_file") #file name sans .epub
		story_file_short = f"{story_file[-story_file_max_length:]}" #the tail end of the filename, if it is long
		progress.set_description(f"{story_file_short:>{story_file_max_length}}") #left pad in case the name is short
		if story_meta["id"] < configuration.start_at:
			progress.update()
			continue
		with zip_file.open(story_meta["archive"]["path"]) as story_epub:
			with catch_warnings():
				simplefilter(action="ignore", category=FutureWarning) # ebooklib/epub.py:1423 xml root element warning
				simplefilter(action="ignore", category=UserWarning) # ebooklib/epub.py:1395 useless warning about ignoring ncx
				book = read_epub(story_epub, {"ignore_ncx": False})
		story = UnanalyzedStory(story_meta, book, first_checked, group_db)
		for doc in story.analyze():
			if stop_event.is_set():
				progress.close()
				return
			if isinstance(doc, Chapter) and any([tag in doc.story.tags for tag in configuration.skip_tags]):
				break
			if isinstance(doc, Story) and any([tag in doc.tags for tag in configuration.skip_tags]):
				break
			index_action = doc.to_dict()
			# e.g. <chapters-{now/d}>
			index_action["_index"] = f"<{doc._index._name[:-1]}" + "{now/d}>"
			waiting = True
			while waiting:
				try:
					es_queue.put(index_action, timeout=0.1)
					waiting = False
				except Full:
					if stop_event.is_set():
						progress.close()
						return
		progress.update()
	stop_event.set()


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
	store_composable_template(Chapter)
	store_composable_template(Story)


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
	print(f"Saving index template for {index_wild} with {nodes} shards")
	conn.indices.put_index_template(name=template_name, template=legacy_index_template, index_patterns=[index_wild])


def bulk_index(es_queue: Queue, stop_event: Event):
	es_client = connections.get_connection()
	docs = doc_conveyor(es_queue, stop_event)
	# https://elasticsearch-py.readthedocs.io/en/stable/helpers.html#elasticsearch.helpers.streaming_bulk
	streamer = streaming_bulk(client=es_client,
								actions=docs,
								chunk_size=50,
								max_chunk_bytes=52428800,
								max_retries=9)
	indexed = 0
	count = 0
	try:
		for ok, action in streamer:
			indexed += ok
			count += 1
		print(f"Indexing completed. {indexed}/{count} successful.")
	except ConnectionTimeout as e:
		print(e)
		stop_event.set()
		return
	except BulkIndexError as e:
		for error_msg in e.errors:
			print(error_msg["error"])
			print(error_msg["data"])
		stop_event.set()
		return


def doc_conveyor(es_queue: Queue, stop_event: Event):
	while True:
		if stop_event.is_set():
			return
		try:
			a_doc = es_queue.get(timeout=0.1)
			yield a_doc
		except Empty:
			pass


def control_c_handler(signal, frame):
	global finished_processing_fics
	if finished_processing_fics.is_set():
		exit(130)
	else:
		finished_processing_fics.set()


def load_config() -> Namespace:
	my_config = Path(__file__).with_suffix(".ini")
	ingest_config = ArgParser(default_config_files=[str(my_config)])
	ingest_config.add_argument('-c', '--config', is_config_file=True, help='config file path')
	api_auth_config = ingest_config.add_argument_group(title="API authentication (will be preferred if both are set)")
	basic_auth_config = ingest_config.add_argument_group(title="Basic authentication")
	api_auth_config.add_argument("--api-id")
	api_auth_config.add_argument("--api-secret")
	basic_auth_config.add_argument("--username")
	basic_auth_config.add_argument("--password")
	ingest_config.add_argument("--es-ca-cert-path", required=True)
	ingest_config.add_argument("--es-hosts", action="append", required=True)
	ingest_config.add_argument("--fimfarchive", type=FileType("rb"), required=True)
	ingest_config.add_argument("--story-count", type=int, default=0)
	ingest_config.add_argument("--start-at", type=int, default=0)
	ingest_config.add_argument("--skip-tags", action="append", default=["Anon", "Anthro", "Advisory"])
	ingest_config.add_argument("--folders-db")
	return ingest_config.parse_args()


if __name__ == "__main__":
	config_options = load_config()
	setup_elasticsearch(config_options)
	# minimum: 0.1 seconds worth of chapters
	# maximum: the time it takes for Elasticsearch to accept one bulk request
	doc_belt = Queue(5)
	finished_processing_fics = Event()
	doc_eater = Thread(target=bulk_index, args=(doc_belt, finished_processing_fics), name="doc eater")
	doc_maker = Thread(target=process_fics, args=(config_options, doc_belt, finished_processing_fics), name="doc maker")
	doc_eater.start()
	doc_maker.start()
	signal(SIGINT, control_c_handler)
	doc_maker.join()
	doc_eater.join()
