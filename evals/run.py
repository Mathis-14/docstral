import argparse
import asyncio
from pathlib import Path
from time import monotonic

from docstral_mcp.config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TOP_K,
    DEFAULT_VESPA_ENDPOINT,
    ServerConfig,
)
from docstral_mcp.qa import AnswerResponse, build_documentation_answerer
from pydantic import BaseModel, Field


class Question(BaseModel):
    id: str = Field(min_length=1, pattern=r"\S")
    query: str = Field(min_length=1, pattern=r"\S")


class Case(BaseModel):
    question: Question
    reference: str | None


class Result(BaseModel):
    question: Question
    reference: str | None
    answer_model: str
    top_k: int
    vespa_endpoint: str
    response: AnswerResponse
    duration_seconds: float


async def run(config: ServerConfig, dataset: Path, output: Path) -> None:
    cases: list[Case] = []
    for line_number, line in enumerate(dataset.read_bytes().splitlines(), start=1):
        try:
            cases.append(Case.model_validate_json(line))
        except ValueError as error:
            error.add_note(f"Invalid evaluation case in {dataset}:{line_number}")
            raise
    if not cases:
        raise ValueError(f"Evaluation dataset is empty: {dataset}")
    if len({case.question.id for case in cases}) != len(cases):
        raise ValueError(
            f"Evaluation dataset contains duplicate question IDs: {dataset}"
        )

    endpoint = str(config.vespa_endpoint).rstrip("/")
    answerer = build_documentation_answerer(
        vespa_endpoint=endpoint, top_k=config.top_k, model=config.answer_model
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        for case in cases:
            started = monotonic()
            try:
                response = await answerer.answer(case.question.query)
            except Exception as error:
                error.add_note(
                    f"Evaluation stopped at {case.question.id}; "
                    f"completed answers are saved in {output}"
                )
                raise
            result = Result(
                question=case.question,
                reference=case.reference,
                answer_model=config.answer_model,
                top_k=config.top_k,
                vespa_endpoint=endpoint,
                response=response,
                duration_seconds=monotonic() - started,
            )
            stream.write(result.model_dump_json() + "\n")
            stream.flush()
            status = "abstained" if response.abstained else "answered"
            print(f"{case.question.id}: {status}")
    print(f"Saved {len(cases)} results to {output}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Save production Q&A answers beside reviewed references."
    )
    parser.add_argument(
        "--dataset", type=Path, default=Path("evals/datasets/qa_dev_v1.jsonl")
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--vespa-endpoint", default=DEFAULT_VESPA_ENDPOINT)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    args = parser.parse_args()
    config = ServerConfig(
        host=DEFAULT_HOST,
        port=DEFAULT_PORT,
        top_k=args.top_k,
        vespa_endpoint=args.vespa_endpoint,
    )
    asyncio.run(run(config, args.dataset, args.output))


if __name__ == "__main__":
    main()
