# Elastic FiMfarchive
This is a script which may be used to import the contents of the [FiMfarchive](https://www.fimfiction.net/user/116950/Fimfarchive) zip file into an [Elasticsearch](https://www.elastic.co/guide/) cluster.

## How to use it:

In general, the steps are to:
1. Download the script into a Python virtual environment
2. Create a read and write user in your Elasticsearch instance
3. Configure the script (see [`index-fics.example.ini`](index-fics.example.ini))
4. Execute `index-fics.py`

## How it works

The script will open the fimfarchive.zip and stream its contents to two indices in Elasticsearch (`chapters-{now/d}` and `stories-{now/d}`).

Most of the metadata from the FiMfarchive's `index.json` is preserved, but *not* all of it.  Some details are added, such as more accurate Wilson scores than what's in the `index.json`, whether a story is deleted, and publishing gaps. See the [class definitions](esdocs.py) for Chapter and Story for what is preserved and added.

This script is particularly distinguished from others like https://github.com/a0346f102085fe9f/IAS2 in that individual chapters are extracted from the .epub and their actual content is associated with their metadata in the `index.json`.

### Detailed setup
This isn't intended to cover fundamentals, but you can learn more about [setting up Elasticsearch](https://www.elastic.co/guide/en/elasticsearch/reference/current/getting-started.html) and a [Python venv](https://docs.python.org/3/library/venv.html).

#### Python
```git clone https://github.com/luna-best/elastic-fimfarchive.git
cd elastic-fimfarchive
python -m venv --system-site-packages --upgrade-deps venv
. venv/bin/activate
pip install -r requirements.txt
```

#### Elasticsearch
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

#### Configuration
See [`index-fics.example.ini`](index-fics.example.ini) for an example configuration.  For authentication, you can choose either the API token mode or the user/pass mode. If you input both, the script will prefer the token mode.  The script provides three ways for skipping content in the zip file:
1. Set the story ID to start at, the script will seek through the zip until it gets to at least that story ID and then begin importing the stories.  By default, the script skips no stories by ID.
2. Select tags to skip.  The tag names match the site's interface; by default the script skips "Anthro" stories.
3. The magic tag "Advisory" for the Foalcon Advisory.  If you don't know what that is, leave it skipped.

#### Various hacking notes:

The script is intended to run on Linux. It might run on Windows, who knows?  Adding threads to the script sped up the indexing speed immensely, but also made it hard to stop.  On Linux, you may have to press Ctrl-C twice to kill it.

The indices it creates are not intended to be preserved. If you run the script twice in quick succession, you will get duplicate entries. You should delete the indices it creates before running it again.  Additionally, it pushes index templates to Elasticsearch on every startup so that you can add more fields to what it should index or, for example, configure it to index the chapter text with a normalizer to take better advantage of Elasticsearch's powerful text search features.