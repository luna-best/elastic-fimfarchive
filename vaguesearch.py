from pathlib import PosixPath
from textwrap import dedent
from nicegui import ui, app
from nicegui.ui import page, run, expansion, label, row, link, textarea, button, number, markdown, input
from nicegui.run import io_bound

from esdocs import Chunk, Story
from rag_cli import setup_elasticsearch, load_config, embed_story, load_csv_maybe
from adapters import STAPIEmbeddings, LlamacppAPI

def startup():
	my_config = load_config()
	app.storage.general.my_config = my_config
	app.storage.general.embedder = STAPIEmbeddings(my_config.vaguesearch["llms"]["embedding"]["stapi host"])
	app.storage.general.llama_cpp_client = LlamacppAPI(my_config.vaguesearch["llms"]["chat"]["host"])
	setup_elasticsearch(my_config)


def relevance_gradient(relevancy: float) -> tuple[int, float, float]:
	if relevancy < 0.5:
		return 225, 1, 1
	# relevancy = 1 -> 50% (blue)
	# relevancy = 0.5 -> 100% (white)
	gradient_pos = -0.5 * relevancy + 1.25
	return 225, 1.0, gradient_pos


def chunk_card(chunk: Chunk, live: bool=True):
	if live:
		template = "https://www.fimfiction.net/story/{story_id}/{chapter}/a/"
	else:
		template = "https://fimfetch.net/story/{story_id}/a/{chapter}"
	with row() as header_row:
		label("Chunk").classes("font-bold")
		label(chunk.order)
		score = label("~:" + f"{chunk.meta.score:.2%}")
		relevance_color = relevance_gradient(chunk.meta.score)
		score.style("color: hsl({}, {:.0%}, {:.0%});".format(*relevance_color))
		if chunk.pcent < 0.25:
			pie = "\N{CIRCLE WITH UPPER RIGHT QUADRANT BLACK}"
		elif chunk.pcent < 0.5:
			pie = "\N{CIRCLE WITH RIGHT HALF BLACK}"
		elif chunk.pcent < 0.75:
			pie = "\N{CIRCLE WITH ALL BUT UPPER LEFT QUADRANT BLACK}"
		else:
			pie = "\N{BLACK CIRCLE}"
		label(f"{pie}{chunk.pcent:.0%} into")
		for chapter in chunk.chapter.number:
			link(f"Ch {chapter}", template.format(story_id=chunk.story.id, chapter=chapter))
	if len(chunk.text) > 80:
		with expansion(f"{chunk.text[0:80]}...") as excerpt:
			if not hasattr(chunk, "to_chat"):
				excerpt.classes("border-2 border-red-500")
			label(chunk.text)
	else:
		label(chunk.text)

def chunk_card2(chunk: Chunk, live: bool=True):
	if live:
		template = "https://www.fimfiction.net/story/{story_id}/{chapter}/a/"
	else:
		template = "https://fimfetch.net/story/{story_id}/a/{chapter}"
	excerpt = chunk.text[0:80]
	with expansion() as chunk_ui:
		with chunk_ui.add_slot("header"):
			with row() as header_row:
				label("Chunk").classes("font-bold")
				label(chunk.order)
				score = label("~:" + f"{chunk.meta.score:.2%}")
				relevance_color = relevance_gradient(chunk.meta.score)
				score.style("color: hsl({}, {:.0%}, {:.0%});".format(*relevance_color))
				if chunk.pcent < 0.25:
					pie = "\N{CIRCLE WITH UPPER RIGHT QUADRANT BLACK}"
				elif chunk.pcent < 0.5:
					pie = "\N{CIRCLE WITH RIGHT HALF BLACK}"
				elif chunk.pcent < 0.75:
					pie = "\N{CIRCLE WITH ALL BUT UPPER LEFT QUADRANT BLACK}"
				else:
					pie = "\N{BLACK CIRCLE}"
				label(f"{pie}{chunk.pcent:.0%} into")
				for chapter in chunk.chapter.number:
					link(f"Ch {chapter}", template.format(story_id=chunk.story.id, chapter=chapter))
			with row() as excerpt_row:
				excerpt_row.style("gap: 3rem")
				label(excerpt)
		if not hasattr(chunk, "to_chat"):
			chunk_ui.classes("border-2 border-red-500")
		if len(chunk.text) > 80:
				label(chunk.text)

def chunk_card3(chunk: Chunk, live: bool=True):
	if live:
		template = "https://www.fimfiction.net/story/{story_id}/{chapter}/a/"
	else:
		template = "https://fimfetch.net/story/{story_id}/a/{chapter}"
	excerpt = chunk.text[0:80]
	with row() as chunk_ui:
		label("Chunk").classes("font-bold")
		label(chunk.order)
		score = label("~:" + f"{chunk.meta.score:.2%}")
		relevance_color = relevance_gradient(chunk.meta.score)
		score.style("color: hsl({}, {:.0%}, {:.0%});".format(*relevance_color))
		if chunk.pcent < 0.25:
			pie = "\N{CIRCLE WITH UPPER RIGHT QUADRANT BLACK}"
		elif chunk.pcent < 0.5:
			pie = "\N{CIRCLE WITH RIGHT HALF BLACK}"
		elif chunk.pcent < 0.75:
			pie = "\N{CIRCLE WITH ALL BUT UPPER LEFT QUADRANT BLACK}"
		else:
			pie = "\N{BLACK CIRCLE}"
		label(f"{pie}{chunk.pcent:.0%} into")
		for chapter in chunk.chapter.number:
			link(f"Ch {chapter}", template.format(story_id=chunk.story.id, chapter=chapter))
			with row():
				label(excerpt)
		if not hasattr(chunk, "to_chat"):
			chunk_ui.classes("border-2 border-red-500")
		if len(chunk.text) > 80:
				label(chunk.text)

def load_chunks(story_id: int, question: str):
	embedding_config = app.storage.general.my_config.vaguesearch["llms"]["embedding"]["prompt"]
	embedder = app.storage.general.embedder
	prompt = dedent(embedding_config["s2p"]).strip() + " "
	prompt += question
	embedded_prompt = embedder.final_embed([prompt])[0]
	try:
		Chunk._index.refresh()
		chunks = Chunk.find_related(story_id, embedded_prompt)
	except ValueError:
		label("No semantically related chunks found.")
		return
	return chunks

def prompt_and_context(chunks: list[Chunk], question: str):
	llama_cpp_client = app.storage.general.llama_cpp_client
	chat_config = app.storage.general.my_config.vaguesearch["llms"]["chat"]
	system_prompt = dedent(chat_config["system prompt"]).strip()
	system_prompt += "\n{chunks}"
	rag_chunks = ""
	chat_tokens = llama_cpp_client.count_tokens(system_prompt.format(chunks=""))
	chat_tokens += llama_cpp_client.count_tokens(question)
	max_prompt = llama_cpp_client.context - chat_config["min answer tokens"]
	for chunk in chunks:
		chat_tokens += llama_cpp_client.count_tokens(chunk.as_blob())
		if chat_tokens > max_prompt:
			break
		chunk.to_chat = True
		rag_chunks += chunk.as_blob()

	system_prompt = system_prompt.format(chunks=rag_chunks)
	#return llama_cpp_client.chat_response(system_prompt + question)
	return llama_cpp_client.chat_response2(system_prompt, question)

async def process_story(story_id: int, semantic_search: str, chat_question: str):
	embedder = app.storage.general.embedder
	title = Story.get_title_lite(story_id)[0:300]
	with expansion(title, value=True) as story_fold:
		placeholder = label("Embedding")
		await io_bound(embed_story, embedder, story_id)
		placeholder.text = "Retrieving"
		try:
			chunks = await io_bound(load_chunks, story_id, semantic_search)
		except ValueError:
			return
		placeholder.text = "Asking..."
		story_fold.update()
		chat_response = await io_bound(prompt_and_context, chunks, chat_question)
		try:
			markdown(chat_response)
		except AttributeError:
			markdown("Failure to convert chat response to markdown")
		for chunk in chunks:
			chunk_card2(chunk)
		placeholder.delete()


def story_maybe(story_id: int):
	try:
		story_title = Story.get_title_lite(story_id)
	except ValueError:
		story_title = "Invalid ID"
	return story_title

def csv_input_validator(path: str):
	csv_in_path=PosixPath(path)
	if not csv_in_path.is_absolute():
		prefix = PosixPath(__file__)
		csv_in_path = prefix / path
	if csv_in_path.is_file():
		return
	return False

def csv_input_enabler(path: str) -> bool:
	try:
		csv_in_path = PosixPath(path)
	except ValueError:
		return False
	if not csv_in_path.is_absolute():
		prefix = PosixPath(__file__)
		csv_in_path = prefix / path
	return csv_in_path.is_file()

def story_titles(story_ids: list[int]) -> list[str]:
	title_list = ""
	for story_id in story_ids:
		try:
			title = f"* {Story.get_title_lite(story_id)}\n"
		except ValueError:
			title = f"* **{story_id} not found!!!**\n"
		title_list += f"{title}"
	return title_list


@page("/")
async def vaguesearch():
	my_config = app.storage.general.my_config
	ui.dark_mode(True) # it can't be in startup() ☹️
	ui.add_css(":root {--nicegui-default-gap: 0.3rem;}")

	with row() as questions_row:
		relevance_question = dedent(my_config.vaguesearch["story"]["one"]["relevance question"]).strip()
		relevance_question = textarea(value=relevance_question, label="Relevance Question")
		relevance_question.props("clearable")
		analysis_question = dedent(my_config.vaguesearch["story"]["one"]["analysis question"]).strip()
		if analysis_question == "relevance question":
			analysis_question = relevance_question.value
		analysis_question = textarea(value=analysis_question, label="Analysis Question")
		analysis_question.props("clearable")

	with row() as one_story_row:
		one_go = button("Search!")
		story_id = number(value=my_config.vaguesearch["story"]["one"]["id"], label="Story ID")
		story_title = label(story_maybe(story_id.value))
		def story_title_color():
			try:
				story_title.text = Story.get_title_lite(story_id.value)
			except ValueError:
				story_title.text = "Invalid ID"
				story_title.style("color: red")
				return
			if Chunk.is_embedded(story_id.value):
				story_title.style("color: green")
			else:
				story_title.style("color: ")
			story_title.update()
		story_id.on("change", story_title_color)
	one_go.on_click(lambda: process_story(story_id.value, relevance_question.value, analysis_question.value))

	with row() as multi_story_row:
		many_go = button("Batch!")
		csv_path = input("Path", value=my_config.vaguesearch["story"]["many"]["list"], validation=csv_input_validator)
		many_go.bind_enabled_from(csv_path, "value", backward=csv_input_enabler)
		many_list = markdown()
		many_list.bind_visibility_from(csv_path, "value", backward=csv_input_enabler)
		def preview_story_list():
			if not csv_input_enabler(csv_path.value):
				return
			csv_maybe = load_csv_maybe(csv_path.value, my_config.vaguesearch["story"]["many"]["id column"])
			if csv_maybe:
				many_list.content = story_titles(csv_maybe)
		preview_story_list()
		csv_path.on_value_change(preview_story_list)


	async def many_go_click():
		many_list.delete()
		many_go.disable()
		for story_id in load_csv_maybe(csv_path.value, my_config.vaguesearch["story"]["many"]["id column"]):
			await process_story(story_id, relevance_question.value, analysis_question.value)
		many_go.enable()
	many_go.on_click(many_go_click)

app.on_startup(startup)
run(host="127.0.0.1", title="Vaguesearch")
