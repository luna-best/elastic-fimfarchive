import logging
from datetime import datetime
from itertools import pairwise
from bs4 import BeautifulSoup

import elasticsearch_dsl as es_dsl_types
from ebooklib.epub import EpubHtml
from statsmodels.stats.proportion import proportion_confint
from typing import Union

class DocStoryAuthor(es_dsl_types.InnerDoc):
	id = es_dsl_types.Integer(meta={"source": "author.id"})
	name = es_dsl_types.Text(meta={"source": "author.name"})

class DocStoryScore(es_dsl_types.InnerDoc):
	ratio = es_dsl_types.Float(meta={"source": "num_likes, num_dislikes from -1 to 1"})
	wilson99 = es_dsl_types.Float(meta={"source": "num_likes, num_dislikes (score field)"})
	wilson97 = es_dsl_types.Float(meta={"source": "num_likes, num_dislikes (97% wilson)"})
	likes = es_dsl_types.Integer(meta={"source": "num_likes"})
	dislikes = es_dsl_types.Integer(meta={"source": "num_dislikes"})

class DocStory(es_dsl_types.InnerDoc):
	author = es_dsl_types.Object(DocStoryAuthor)
	words = es_dsl_types.Integer(meta={"source": "num_words"})
	completion_status = es_dsl_types.Keyword(meta={"source": "completion_status"})
	content_rating = es_dsl_types.Keyword(meta={"source": "content_rating"})
	id = es_dsl_types.Integer(meta={"source": "id"})
	score = es_dsl_types.Object(DocStoryScore)
	tags = es_dsl_types.Keyword(multi=True, meta={"source": "tags.name"})
	title = es_dsl_types.Text(meta={"source": "title"})
	published = es_dsl_types.Date(meta={"source": "date_published"})
	views = es_dsl_types.Long(meta={"source": "num_views"})

class DocChapter(es_dsl_types.InnerDoc):
	number = es_dsl_types.Short(meta={"source": "chapters.chapter_number or epub"})
	published = es_dsl_types.Date(meta={"source": "chapters.date_published"})
	words = es_dsl_types.Integer(meta={"source": "chapters.num_words"})
	id = es_dsl_types.Integer(meta={"source": "chapters.id"})
	title = es_dsl_types.Text(meta={"source": "epub or chapters.title"})
	text = es_dsl_types.Text(meta={"source": "epub"})
	ghost = es_dsl_types.Text(meta={"source": "epub and chapters"})

class Chapter(es_dsl_types.Document):
	chapter = es_dsl_types.Object(DocChapter)
	story = es_dsl_types.Object(DocStory)

	class Index:
		name = "chapters-*"
		settings = {
			#"codec": "best_compression", #compression slows it down by 1/5 and reduces the index size by 1/3
			"number_of_replicas": 0,
			#"refresh_interval": "60s",
			"query.default_field": "story.title"
		}

	def calculate_scores(self, up: int, down: int):
		votes = up + down
		if votes <= 0:
			return
		self.story.score.likes = up
		self.story.score.dislikes = down
		self.story.score.ratio = (up - down) / votes

		lower, upper = proportion_confint(up, votes, 0.01, method="wilson")
		self.story.score.wilson99 = lower
		lower, upper = proportion_confint(up, votes, 0.03, method="wilson")
		self.story.score.wilson97 = lower

	@staticmethod
	def bayesian_credible_interval(
			up: int,
			down: int,
			tail_prob: float = 0.05,
			prior_a: float = 1.0,
			prior_b: float = 1.0,
		) -> tuple[float, float, float]:
		import numpy as np
		from scipy.stats import beta
		up = np.float64(up)
		down = np.float64(down)
		tail_prob = np.float64(tail_prob)
		prior_a = np.float64(prior_a)
		prior_b = np.float64(prior_b)
		posterior_a = prior_a + up
		posterior_b = prior_b + down
		half_tail_prob = 0.5 * tail_prob
		left_endpoint = beta.ppf(half_tail_prob, posterior_a, posterior_b)
		right_endpoint = beta.isf(half_tail_prob, posterior_a, posterior_b)
		posterior_mean = posterior_a / (posterior_a + posterior_b)
		return posterior_mean, left_endpoint, right_endpoint

	def fill_story_author_meta(self, story_meta: dict):
		self.story.author.id = story_meta["author"]["id"]
		self.story.author.name = story_meta["author"]["name"]
		self.story.words = story_meta["num_words"]
		self.story.completion_status = story_meta["completion_status"]
		self.story.content_rating = story_meta["content_rating"]
		self.story.id = story_meta["id"]
		self.story.published = story_meta["date_published"]
		self.story.views = story_meta["num_views"]
		self.story.tags = [ tag["name"] for tag in story_meta["tags"]]
		self.story.title = story_meta["title"]
		self.calculate_scores(story_meta["num_likes"], story_meta["num_dislikes"])
		#from -1 as perfect dislike ratio to +1 as perfect like ratio, no rating as null
		if any(reaction > 0 for reaction in [story_meta["num_dislikes"], story_meta["num_likes"]]):
			self.story.score.ratio = (story_meta["num_likes"] - story_meta["num_dislikes"]) / \
								(story_meta["num_likes"] + story_meta["num_dislikes"])

	def fill_chapter_meta_full(self, title: str, number: int, chapter_data: dict):
		self.chapter.title = title
		self.chapter.number = number + 1
		#kibana needs the chapter publish date to be useful. it will always be set, even when the data is missing
		if self.story.published: # first fallback: story publish date
			self.chapter.published = self.story.published
		if chapter_data["date_published"]:
			self.chapter.published = chapter_data["date_published"]
		if self.chapter.published is None: # second fallback is index time
			self.chapter.published = datetime.utcnow()
		self.chapter.words = chapter_data["num_words"]
		self.chapter.id = chapter_data["id"]
	
	def fill_chapter_meta_sparse(self, title: str, number: int, ghost_message: str):
		self.chapter.title = title
		self.chapter.number = number #it is decremented in analyze() below
		self.chapter.ghost = ghost_message
		# it's already known that the publish date is missing, go straight to the final fallback
		self.chapter.published = datetime.utcnow()

	@staticmethod
	def try_to_find_title(chapter_dom) -> Union[str, None]:
		for h1 in chapter_dom.body.find_all("h1"):
			if h1.text == "Author's Note":
				continue
			else:
				title = h1.text
				h1.clear() #small % chance to remove an actual in-story <h1>... meh
				return title

	def analyze(self, chapter, story_meta: dict, chapters_data: list):
		self.fill_story_author_meta(story_meta)
		if chapter.number >= 0:
			self.fill_chapter_meta_full(chapter.title, chapter.number, chapters_data[chapter.number])
		else:
			if type(chapter.href) is list:
				ghost_message = (f"Ghost multichapter > Author: {story_meta['author']['name']}|"
							 f"Story: {story_meta['url']}|"
							 f"epub: {story_meta['archive']['path']}|"
							f"chapters: {[chapter.file_name for chapter in chapter.href]}")
			else:
				ghost_message = (f"Ghost chapter > Author: {story_meta['author']['name']}|"
								 f"Story: {story_meta['url']}|"
								 f"epub: {story_meta['archive']['path']}|"
								 f"chapter: {chapter.href.file_name}|")
			logging.warning(ghost_message)
			self.fill_chapter_meta_sparse(chapter.title, chapter.number, ghost_message)
		if type(chapter.href) is list:
			self.eat_multi_chapter(chapter.href, chapter.title)
		else:
			self.eat_simple_chapter(chapter.href, chapter.title)

	def eat_multi_chapter(self, chapters: list[EpubHtml], title: str):
		first_chapter_dom = BeautifulSoup(chapters[0].get_content(), "lxml-xml")
		self.try_to_remove_title(first_chapter_dom, title)
		for chapter in chapters[1:]:
			next_chapter_dom = BeautifulSoup(chapter.get_content(), "lxml-xml")
			self.try_to_remove_title(next_chapter_dom, title)
			first_chapter_dom.body.extend(next_chapter_dom.body)
		self.chapter.text = first_chapter_dom.body.get_text(" ")  # da magics

	def eat_simple_chapter(self, chapter: EpubHtml, title: str):
		chapter_dom = BeautifulSoup(chapter.get_content(), "lxml-xml")
		self.try_to_remove_title(chapter_dom, title)
		self.chapter.text = chapter_dom.body.get_text(" ")  # da magics


	@staticmethod
	def try_to_remove_title(chapter_dom, title: str):
		for h1 in chapter_dom.body.find_all("h1"):
			if h1.text == title:
				h1.clear()


class DocStoryDescription(es_dsl_types.InnerDoc):
	short = es_dsl_types.Text(meta={"source": "short_description"})
	long = es_dsl_types.Text(meta={"source": "description_html"})


class Story(es_dsl_types.Document):
	author = es_dsl_types.Object(DocStoryAuthor)
	words = es_dsl_types.Integer(meta={"source": "num_words"})
	completion_status = es_dsl_types.Keyword(meta={"source": "completion_status"})
	content_rating = es_dsl_types.Keyword(meta={"source": "content_rating"})
	score = es_dsl_types.Object(DocStoryScore)
	tags = es_dsl_types.Keyword(multi=True, meta={"source": "tags.name"})
	title = es_dsl_types.Text(meta={"source": "title"})
	published = es_dsl_types.Date(meta={"source": "date_published"})
	views = es_dsl_types.Long(meta={"source": "num_views"})
	id = es_dsl_types.Integer(meta={"source": "id"})
	description = es_dsl_types.Object(DocStoryDescription)
	deleted = es_dsl_types.Boolean(meta={"source": "archive.date_fetched"})
	publish_gaps = es_dsl_types.IntegerRange(meta={"source": "chapters.date_published"})

	class Index:
		name = "stories-*"
		settings = {
			"number_of_replicas": 0,
			#"refresh_interval": "60s",
			"query.default_field": "title",
		}

	def analyze(self, source: Chapter, story_meta: dict, archive_date: datetime):
		direct_copies = ["author", "words", "completion_status", "content_rating", "score", "tags", "title", "published", "views", "id"]
		for attr in direct_copies:
			setattr(self, attr, getattr(source.story, attr))

		if story_meta["description_html"]:
			desc_dom = BeautifulSoup(story_meta["description_html"], "html.parser") # likely consist of a single <p>
			self.description.long = desc_dom.text
		self.description.short = story_meta["short_description"]

		try:
			date_checked = datetime.fromisoformat(story_meta["archive"]["date_fetched"])
			checked_difference = archive_date - date_checked
			self.deleted = checked_difference.days > 30
		except TypeError:
			self.deleted = True

		publish_dates = [
			datetime.fromisoformat(chapter["date_published"])
			for chapter in story_meta["chapters"]
			if chapter["date_published"]
		]
		if len(publish_dates) > 1:
			publish_dates.sort(reverse=True)
			gaps = [
				delta.days
				for delta in map(lambda pair: pair[0] - pair[1], pairwise(publish_dates))
			]
			self.publish_gaps = {
				"lte": max(gaps),
				"gte": min(gaps)
			}




