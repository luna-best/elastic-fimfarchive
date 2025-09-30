# Semantic Search

This script may be used to search a story for passages which are related to a question; the question may 
optionally be answered by an LLM based on the question and chunks (RAG, defined below). 

## Dependencies

Be warned, the implementation of semantic search is quite janky, as few of the tools used for this actually worked
without poking, prodding and patching.  The easiest dependency is probably the python one: 
`pip install chonkie[semantic]` (in the virtual environment for elastic-fimfarchive)

### Elasticsearch

The script expects that the contents of the story are indexed in Elasticsearch, as described in the main readme.
It retrieves story content to be embedded from the `chapters-*` index pattern.  In addition to the reading `chapters-*`,
this script writes to the `chunks-*` index pattern, and requires the following permissions on that pattern:
* monitor
* auto_configure
* write
* create_index
* view_index_metadata
* read
* maintenance

### STAPI

The embedding model I chose, [stella](https://huggingface.co/dunzhang/stella_en_400M_v5), is hard to use at 
abstraction layers above sentence-transformers. As such, the simple 
[Sentence Transformers API](https://github.com/substratusai/stapi) is used as the backend server which provides
embeddings.  Even so, STAPI does not work correctly due to the way stella functions. Specifically, stella's embedder
always pads or truncates input to a fixed number of tokens, while the chunking algorithm must know the number of tokens
that are in a block of text to work.  Consequently, STAPI is patched to correctly return the number of tokens 
in its input.

1. Make a Python virtual environment
   1. `python3.12 -m venv --system-site-packages --upgrade-deps --prompt stapi stapi`
   2. `cd stapi`
   3. `source ./bin/activate`
2. Download STAPI
   1. `git init --initial-branch=main`
   2. `git remote add gh https://github.com/substratusai/stapi.git`
   3. `git fetch gh main`
   4. `git checkout main`
3. Install dependencies (xformers is required by the model, not STAPI)
   1. `pip install --requirement requirements.txt xformers` 
4. add tokenization endpoints to STAPI
   1. `wget https://raw.githubusercontent.com/luna-best/elastic-fimfarchive/refs/heads/main/stapi.patch`
   2. `patch -p1 < stapi.patch`
5. run STAPI: `MODEL=dunzhang/stella_en_400M_v5 uvicorn --host ${HOST} --port ${PORT} main:app`
6. put the HOST and PORT in `vaguesearch.toml` under `llms.embedding.stapi host` as a URL

<details>
<summary>STAPI systemd unit</summary>

`systemctl --user edit --full --force stapi.service`
```text
[Unit]
Description=stapi

[Service]
Type=simple
ExecStart=/.../stapi/bin/uvicorn --host ... --port ... --env-file stella.env main:app
WorkingDirectory=/.../stapi
Environment="MODEL=dunzhang/stella_en_400M_v5"
```
</details>

### Llama.cpp
Theoretically, STAPI could be patched to function as a chat server, but it is a lot more work.  Llama.cpp is much 
easier to start up and interact with than alternatives (such as patching Ollama to provide a tokenize endpoint).

The basic [installation instructions](https://github.com/ggml-org/llama.cpp?tab=readme-ov-file#quick-start) may be 
followed, and the model I chose is [Falcon-H1-3B](https://huggingface.co/tiiuae/Falcon-H1-3B-Instruct-GGUF)

The URL to the chat server is placed in `vaguesearch.toml` under `llms.chat.host`
Next, place the maximum amount of context that the RAG will use. This is the system prompt, query, and chunk content,
after which additional chunks will not be sent to Llama.cpp. If the model has only a little context, be sure to leave 
some room for the model's answer.

<details>
<summary>Example user unit for Llama.cpp:</summary>

```text
[Unit]
Description=LLama.cpp server

[Service]
Type=simple
ExecStart=/.../llama.cpp/build/bin/llama-server
WorkingDirectory=/.../llama.cpp
Environment="LLAMA_ARG_HF_REPO=tiiuae/Falcon-H1-3B-Instruct-GGUF"
Environment="LLAMA_ARG_HOST=..."
Environment="LLAMA_ARG_PORT=..."
```

</details>

## Running the search

In `vaguesearch.toml`, under the section `story.one`, set `id` to the story ID you want to query.

Write a question about the story under `relevance question`. The results are quite good, even if the question
is written poorly. However, it can help to be mindful of the way stella was trained, and its s2p prompt: 
"Instruct: Given a web search query, retrieve relevant passages that answer the query. Query:"

Write `analysis question` or put "relevance question" in that field to copy it.  Generally, the question is 
answered poorly.

Run the script as `python rag_cli.py --vaguesearch vaguesearch.toml`

The output will be saved to `ragout.txt` with the following sections in order:
* The analysis question
* The chat LLM's response
* The chunks which were found to be related to the question

The chunk header fields are:
* The chunk's order in the story
* The similarity score as a floating point
* The distance into the chapter in which the chunk exists (e.g. 0% at the beginning, 99% at the end)
* The chapter link on FiMFiction if it is not deleted, else FiMFetch (for convenient manual review)

## RAG process

The script works with the following steps, which is the basic definition of RAG:
1. Load the contents of a story as plain text into memory.
2. Break the contents into chunks that fit within the context window of the embedding model chosen
3. Embed the chunks with the embedding model
4. Store the chunks in Elasticsearch with metadata required to reassemble the text used to create them
5. Embed a question about the story using the model's "similarity to prompt" embedding mode
6. Search Elasticsearch for chunks similar to the question's embedding
7. Reconstruct the text from the chunk metadata
8. Construct a prompt for the chat LLM which combines:
   * Instructions to answer the question
   * The question
   * The chunk number and text (only) for each chunk up to the context window limit of 5252
   * (This means that not all chunks in the output file were necessarily sent to the LLM)
9. Send the prompt to the chat LLM and retrieve the reply
10. Format the output to include relevant metadata and save it to the file