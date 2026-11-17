"""Pydantic V2 strict tool schema registry."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)
ToolHandler = Callable[[BaseModel, Any], Any]
ToolPreview = Callable[[BaseModel, Any], Any]


@dataclass(slots=True, frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: ToolHandler
    is_read_only: bool = False
    is_concurrency_safe: bool = False
    risk_tier: int = 3
    preview: ToolPreview | None = None

    def strict_schema(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema(mode="validation")
        return _strict_schema(schema)

    async def invoke(self, data: BaseModel, context: Any) -> Any:
        result = self.handler(data, context)
        if inspect.isawaitable(result):
            return await result
        return result


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        description: str,
        input_model: type[BaseModel],
        handler: ToolHandler,
        *,
        is_read_only: bool = False,
        is_concurrency_safe: bool = False,
        risk_tier: int = 3,
        preview: ToolPreview | None = None,
    ) -> ToolSpec:
        if not name or name in self._tools:
            raise ValueError(f"tool name is empty or already registered: {name!r}")
        if not inspect.isclass(input_model) or not issubclass(input_model, BaseModel):
            raise TypeError("input_model must be a Pydantic BaseModel subclass")
        if risk_tier not in {1, 2, 3}:
            raise ValueError("risk_tier must be 1, 2, or 3")
        spec = ToolSpec(name, description, input_model, handler, is_read_only, is_concurrency_safe, risk_tier, preview)
        self._tools[name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self):
        return iter(self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.strict_schema(),
                "strict": True,
            }
            for spec in self._tools.values()
        ]

    def openai_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.strict_schema(),
                    "strict": True,
                },
            }
            for spec in self._tools.values()
        ]

    def anthropic_tools(self) -> list[dict[str, Any]]:
        return [
            {"name": spec.name, "description": spec.description, "input_schema": spec.strict_schema()}
            for spec in self._tools.values()
        ]

    def parse_arguments(self, spec: ToolSpec, arguments: Mapping[str, Any] | str) -> BaseModel:
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON arguments: {exc.msg}") from exc
        if not isinstance(arguments, Mapping):
            raise ValueError("tool arguments must be an object")
        normalized = dict(arguments)
        if set(normalized) == {"arguments"} and isinstance(normalized["arguments"], Mapping):
            normalized = dict(normalized["arguments"])
        schema = spec.strict_schema()
        properties = set(schema.get("properties", {}))
        extra = set(normalized) - properties
        if extra:
            raise ValueError(f"unknown tool argument(s): {', '.join(sorted(extra))}")
        try:
            return spec.input_model.model_validate(normalized)
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc


def _strict_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    defs = dict(schema.get("$defs", {}))

    def resolve(value: Any) -> Any:
        if isinstance(value, Mapping):
            if "$ref" in value:
                target = str(value["$ref"]).split("/")[-1]
                return resolve(defs.get(target, {}))
            output = {key: resolve(item) for key, item in value.items() if key not in {"$defs", "$ref"}}
            if output.get("type") == "object" or "properties" in output:
                output.setdefault("type", "object")
                output["additionalProperties"] = False
                properties = output.get("properties", {})
                output["required"] = sorted(properties)
                output["properties"] = {key: resolve(item) for key, item in properties.items()}
            return output
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return value

    result = resolve(dict(schema))
    result.setdefault("type", "object")
    result["additionalProperties"] = False
    result["required"] = sorted(result.get("properties", {}))
    return result
