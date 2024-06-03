from pathlib import Path
from struct import iter_unpack
from lzma import decompress
from base64 import b64decode

from typing import Iterator


# this is a compressed list of fics from the "foalcon advisory," see below for how it is generated
skipblob = """
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"""


def generate_skips(blob: str = skipblob) -> Iterator[int]:
	compressed_blob_b64 = blob.encode("ascii")
	compressed_blob = b64decode(compressed_blob_b64)
	bytestream = decompress(compressed_blob)
	encoded_skips = iter_unpack("<H", bytestream)
	first_id = next(encoded_skips)[0]
	yield first_id
	for adds in encoded_skips:
		first_id += adds[0]
		yield first_id


if __name__ == "__main__":
	"""
	In order for this code to work correctly, you need to have imported the fimfarchive without skipping any stories,
	especially without the Advisory "magic" tag.  The script reads the advisory from disk, but it can be saved from,
	e.g.: https://web.archive.org/web/20240419223159/https://fimfetch.net/foalcon-advisory
	Then it searches for the author ID - title pairs, with a little allowance for fuzziness
	Any found pairs are stored as a list of story IDs, and then transformed into a blob which can be pasted in code
	"""
	from tomllib import loads
	from itertools import pairwise
	from struct import pack
	from lzma import compress
	from base64 import encodebytes
	from bs4 import BeautifulSoup
	from esdocs import Story
	from elasticsearch_dsl import connections
	from Levenshtein import ratio #pip install levenshtein

	my_config_path = Path(__file__).with_suffix(".ini")
	my_config = loads(my_config_path.read_text())

	"""
	example advisory_skipper.ini:
	advisory_html = "foalcon-advisory.html"

	[elasticsearch]
	hosts = [ "https://some-host:9200" ]
	ca_cert = "/your/http_ca.crt"
	username = "elasticsearch reader username"
	password = "elasticsearch reader password"
	"""

	connections.create_connection(hosts=my_config["elasticsearch"]["hosts"],
									ca_certs=my_config["elasticsearch"]["ca_cert"],
									basic_auth=(my_config["elasticsearch"]["username"], my_config["elasticsearch"]["password"]))
	advisory = Path(my_config["advisory_html"])
	soup = BeautifulSoup(advisory.read_text(), "lxml-xml")
	fimfiction_section = soup.find_all("table", "userlist")[0]
	ids_found = []
	failures = []
	story_search = Story._index.search().source(includes=["title", "id"])
	for row in fimfiction_section.tbody.find_all("tr"):
		columns = row.find_all("td")
		author_id = int(columns[0].text)
		search_author = story_search.filter("term", author__id=author_id)
		for story in columns[2].find_all("span", "title"):
			story_title = story.text
			title_query = search_author.query("match", title=story_title)
			res = title_query.execute()
			if not res.hits.total.value:
				failures.append({"id": author_id, "title": story_title, "hits": []})
				continue
			found = False
			for hit in res.hits.hits:
				# find about a dozen more fics due to title differences, especially unicode and special characters that break exact matches
				closeness = ratio(story_title, hit._source["title"], score_cutoff=0.8)
				if closeness > 0.8:
					ids_found.append(hit._source["id"])
					found = True
					# if closeness < 1:
					# 	print(f'Searched: {story_title}, Matched: {hit._source["title"]}, ratio: {closeness}')
					break
			if not found:
				# manual review - 298 not found out of 1608 fics in the list, looks like only a few are missed. examples:
				# possible failure "An Afternoon of Discovery" -> "An Evening of Discovery"
				# likely success: "When Things Change [Deleted]" -> "When Things Change (Deleted Scenes)"
				# probably the 298 failures actually deleted and not in fimfarchive. good enough
				failures.append({"id": author_id, "title": story_title, "hits": [hit._source["title"] for hit in res.hits.hits]})
	ids_found.sort() # thanks to the FiMFarchive for being ordered sequentially by story ID for this trick
	first = ids_found[0]
	deltas = list(map(lambda pair: pair[1] - pair[0], pairwise(ids_found)))
	compress_this = [first]
	compress_this.extend(deltas)
	blob = pack(f"<{len(compress_this)}H", *compress_this)
	compressed_blob = compress(blob)
	b64 = encodebytes(compressed_blob).decode("ascii")
	print(b64)
