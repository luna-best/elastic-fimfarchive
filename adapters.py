from dataclasses import dataclass
from typing import Sequence, Optional, Union, Mapping, Any

from chonkie import BaseEmbeddings
from numpy import ndarray, array, matmul, linalg

from ollama import Options, Client
from ollama._types import BaseGenerateResponse, BaseRequest
from requests import Session


class TokenizeResponse(BaseGenerateResponse):
	tokens: Sequence[int]


class TokenizeRequest(BaseRequest):
	prompt: str
	keepalive: Optional[Union[float, str]] = None
	options: Optional[Union[Mapping[str, Any], Options]] = None


class DeTokenizeResponse(BaseGenerateResponse):
	text: str


class DeTokenizeRequest(BaseRequest):
	tokens: Sequence[int]
	keepalive: Optional[Union[float, str]] = None
	options: Optional[Union[Mapping[str, Any], Options]] = None


class PatchedClient(Client):
	def tokenize(self, model: str = '', prompt: str = "", options=None, keepalive=None) -> TokenizeResponse:
		req = TokenizeRequest(model=model, prompt=prompt, keepalive=keepalive, options=options)
		resp = self._request(TokenizeResponse, "POST", "/api/tokenize", json=req.model_dump(exclude_none=True))
		return resp

	def detokenize(self, model: str = "", tokens: Sequence[int] = None, options=None, keepalive=None)-> DeTokenizeResponse:
		req = DeTokenizeRequest(model=model, tokens=tokens, keepalive=keepalive, options=options)
		resp = self._request(DeTokenizeResponse, "POST", "/api/detokenize", json=req.model_dump(exclude_none=True))
		return resp


@dataclass
class STAPIEmbeddings(BaseEmbeddings):
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

	def final_embed(self, texts: list[str]) -> list[float]:
		resp = self._embed(texts)
		embeddings = [
			embedding["embedding"]
			for embedding in resp["data"]
		]
		return embeddings
