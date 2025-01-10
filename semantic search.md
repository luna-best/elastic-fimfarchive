# Semantic Search

This script may be used to search a story for passages which are related to a question; the question may 
optionally be answered by an LLM based on the question and chunks (RAG, defined below). 

## Dependencies

Be warned, the implementation of semantic search is quite janky, as none of the tools used for this actually worked
without poking, prodding and patching.

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
2. `pip install stapi xformers`
3. add tokenization endpoints to stapi: `patch -p1 < stapi.patch`
4. run stapi: `MODEL=dunzhang/stella_en_400M_v5 uvicorn --host ${HOST} --port ${PORT} main:app`
5. put the HOST and PORT in `vaguesearch.toml` under `llms.embedding.stapi host` as a URL

<details>
<summary>STAPI systemd unit</summary>

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

### Ollama
Theoretically, STAPI could be patched to function as a chat server, but it is a lot more work.  Ollama is much easier
to work with as a chat server, but it is still critical to count tokens in a low VRAM environment, and that feature is 
[not provided](https://github.com/ollama/ollama/issues/3582).  Even so, a 
[patch exists](https://github.com/ollama/ollama/pull/7412) to provide that feature.  So, Ollama will be patched and
built from source:
1. `git clone https://github.com/ollama/ollama.git`
   * Note: if you have old `go` (check `go.mod`), run: `git clone --branch v0.5.1 https://github.com/ollama/ollama.git`
2. `cd ollama`
   * Note: if you used `--branch` above, add a step: `git switch --create v0.5.1`
3. `git fetch origin pull/7412/head:tokenizer-endpoints`
4. `git merge tokenizer-endpoints -m "git is miserable"`
5. [Build Ollama](https://github.com/ollama/ollama/blob/main/docs/development.md#linux)
   1. Install build prerequisites
   2. It's unnecessary to build for both CUDA 11 and 12, you can build for the version you have
   3. Additionally, you can select your own [graphics card architecture](https://developer.nvidia.com/cuda-gpus#collapse2).
   4. `make cuda_v11 CUDA_ARCHITECTURES=61`
   5. `go build .`
6. Run Ollama
   1. `OLLAMA_HOST=${HOST} ./ollama serve`
   2. Note: Ollama serves on port 11434 by default
7. Put the Ollama URL in `vaguesearch.toml` under `llms.chat.ollama host`
8. Put a [model](https://ollama.com/library) in `vaguesearch.toml` under `llms.chat.ollama model`
   1. Such as  `falcon3:1b-instruct-fp16` or `hf.co/bartowski/Llama-3.2-1B-Instruct-GGUF:F16`

<details>
<summary>Ollama systemd unit</summary>

```text
[Unit]
Description=ollama

[Service]
Type=simple
ExecStart=/.../ollama serve
Environment="OLLAMA_HOST=..."

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