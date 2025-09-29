from dataclasses import dataclass
from typing import  Union, Any, Callable

from chonkie import BaseEmbeddings
from numpy import ndarray, array, matmul, linalg

from requests import Session



@dataclass
class STAPIEmbeddings(BaseEmbeddings):
	def get_tokenizer_or_token_counter(self) -> Union[Any, Callable[[str], int]]:
		return self.count_tokens

	host: str

	def __post_init__(self):
		self.sesh = Session() # for keep-alive

	def _embed(self, text: Union[str, list[str]]):
		body = {
			"model": "ignored",
			"input": text
		}
		resp = self.sesh.post(f"{self.host}/v1/embeddings", json=body)
		return resp.json()

	def count_tokens(self, text: str) -> int:
		body = {
			"model": "ignored",
			"input": text
		}
		resp = self.sesh.post(f"{self.host}/v1/tokenize", json=body)
		return resp.json()["usage"]["prompt_tokens"]

	def embed(self, text: str) -> ndarray:
		resp = self._embed(text)
		embedded = resp["data"][0]["embedding"]
		return array([embedded])

	def embed_batch(self, texts: list[str]) -> list[ndarray]:
		resp = self._embed(texts)
		embeddings = [
			array(embedding["embedding"])
			for embedding in resp["data"]
		]
		return embeddings

	def count_tokens_batch(self, texts: list[str]) -> list[int]:
		body = {
			"model": "ignored",
			"input": texts
		}
		resp = self.sesh.post(f"{self.host}/v1/tokenize", json=body)
		counts = [
			len(tokens["embedding"])
			for tokens in resp.json()["data"]
		]
		return counts
		#return super().count_tokens_batch(texts)

	def similarity(self, u: ndarray, v: ndarray) -> float:
		# https://numpy.org/doc/stable/reference/generated/numpy.dot.html#numpy.dot
		# If both a and b are 2-D arrays, it is matrix multiplication, but using matmul or a @ b is preferred.
		numerator = matmul(u, v.T) #i do not understand why it needs .T
		denominator = linalg.norm(u) * linalg.norm(v)
		return numerator / denominator

	@property
	def dimension(self) -> int:
		return 1024

	def final_embed(self, texts: list[str]) -> list[list[int]]:
		resp = self._embed(texts)
		embeddings = [
			embedding["embedding"]
			for embedding in resp["data"]
		]
		return embeddings

@dataclass
class LlamacppAPI:
	base_url: str

	def __post_init__(self):
		self.base_url = self.base_url.rstrip("/")
		self.session = Session()

	def tokenize(self, content: str) -> list[int]:
		resp = self.session.post(f"{self.base_url}/tokenize", json={"content": content})
		return resp.json()["tokens"]

	def count_tokens(self, content: str) -> int:
		return len(self.tokenize(content))

	def completion(self, prompt: Union[str, list[str], list[int], dict[str, Any]]) -> dict[str, str]:
		resp = self.session.post(f"{self.base_url}/completion", json={"prompt": prompt})
		return resp.json()

	def chat_response(self, prompt: Union[str, list[str], list[int], dict[str, Any]]) -> str:
		return self.completion(prompt)["content"]