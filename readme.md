# Elasticsearch FiMfarchive
This is a script which may be used to import the contents of the [FiMfarchive](https://www.fimfiction.net/user/116950/Fimfarchive) zip file into an [Elasticsearch](https://www.elastic.co/guide/) cluster.

## How to use it:

In general, the steps are to:
1. Download the script into a Python virtual environment.
2. Create a write user in your Elasticsearch instance for the script and a read user for yourself.
3. Configure the script (see [`index-fics.example.ini`](index-fics.example.ini)) or `--help`.
4. Execute `index-fics.py`.

## How it works

The script will read the `fimfarchive.zip` and stream its contents to two indices in Elasticsearch (`chapters-{now/d}` and `stories-{now/d}`).

*Most* of the metadata from the FiMfarchive's `index.json` is preserved.  Some statistics are added, such as more accurate Wilson scores than what's in the `index.json`, whether a story is deleted, and publishing gaps. See the [class definitions](esdocs.py) for Chapter and Story for what is preserved and added.

This script is particularly distinguished from others like https://github.com/a0346f102085fe9f/IAS2 in that individual chapters are extracted from the .epub and their actual content is associated with their metadata in the `index.json`.

## Detailed setup
This guide isn't intended to cover fundamentals, but you can learn more about [setting up Elasticsearch](https://www.elastic.co/guide/en/elasticsearch/reference/current/getting-started.html) and a [Python venv](https://docs.python.org/3/library/venv.html).

### Python
```bash
git clone https://github.com/luna-best/elastic-fimfarchive.git
cd elastic-fimfarchive
python -m venv --system-site-packages --upgrade-deps venv
. venv/bin/activate
pip install -r requirements.txt
```

### Elasticsearch
The script requires a user or API key that has the following permissions:

* [Cluster privileges](https://www.elastic.co/guide/en/elasticsearch/reference/current/security-privileges.html#privileges-list-cluster):
	* monitor
	* manage_index_templates

* [Index privileges](https://www.elastic.co/guide/en/elasticsearch/reference/current/security-privileges.html#privileges-list-indices) on both `chapters-*` and `stories-*`:
	* monitor
	* auto_configure
	* write
	* create_index
	* view_index_metadata

Create a [space](https://www.elastic.co/guide/en/kibana/current/xpack-spaces.html) and [import](https://www.elastic.co/guide/en/kibana/current/managing-saved-objects.html) the [Data Views](data%20views.ndjson) for a better experience in Kibana. Useful advanced space settings:

| Setting                   | Value                                                     |
|---------------------------|-----------------------------------------------------------|
| `timepicker:timeDefaults` | `{  "from": "2011-07-08T18:04:11+00:00",  "to": "now"}`   |
| `timepicker:quickRanges`  | `[]`                                                      |
| `defaultColumns`          | `story.author.name, story.title, story.id, chapter.title` |

When searching for chapters by their content, it's helpful to add the meta field `_score` to the sorted fields and remove the publish date.

### Configuration
See [`index-fics.example.ini`](index-fics.example.ini) for an example configuration.  All configuration settings are accepted as command line options as well, run `python index-fics.py --help` to see them. For authentication, you can choose either the API token mode or the user/pass mode. If you input both, the script will prefer the token mode.  There are three ways for skipping content in the zip file:
1. Set the story ID to start at, the script will seek through the zip until it gets to at least that story ID and then begin importing the stories.  By default, the script skips no stories by ID.
2. Select tags to skip.  The tag names match the site's interface. By default the script skips "Anon" and "Anthro" stories.
3. The magic tag "Advisory" for the Foalcon Advisory, which is skipped.  If you don't know what that is, leave it skipped.

## Hacking notes:

The script is intended to run on Linux. It might run on Windows, who knows?  Adding threads to the script sped up the indexing speed immensely, but also made it hard to stop.  On Linux, you may have to press Ctrl-C twice to kill it.

The indices it creates are intended to be ephemeral. If you run the script twice in quick succession, you will get duplicate entries in the same index. In general, you should delete the indices it creates before running it again.  Additionally, it pushes index templates to Elasticsearch on every startup so that you can add more fields to what it should index or, for example, configure it to index the chapter text with a normalizer to take better advantage of Elasticsearch's powerful text search features.  Finally, not all chapters have the publish metadata that Kibana depends on. If it can't be sanely guessed, that field is set to the time of ingest.

The indexing process takes a while, there are a lot of knobs available to turn for increasing its performance.  In particular, check the Elasticsearch [connection](https://github.com/luna-best/elastic-fimfarchive/blob/7b7b51b639321ca7f8f91a88c00f88c3cbca3ac8/index-fics.py#L216) settings, the [bulk index](https://github.com/luna-best/elastic-fimfarchive/blob/7b7b51b639321ca7f8f91a88c00f88c3cbca3ac8/index-fics.py#L247) settings and the [index](https://github.com/luna-best/elastic-fimfarchive/blob/7b7b51b639321ca7f8f91a88c00f88c3cbca3ac8/esdocs.py#L49) settings.  After a full ingest with no skips at all, the indices take about 16GB of space.  The script seems to use about 300-400 MB of RAM while running.

I'm not the creator of the FiMfarchive, I just use it for fun.