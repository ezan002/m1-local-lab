"""studio-executor: let Agent Studio debug a LangGraph agent that keeps running in its own process.

    from studio_executor import StudioExecutor, ForkField, serve

    graph = builder.compile(checkpointer=SqliteSaver(...))          # your graph, your checkpointer
    executor = StudioExecutor(graph, name="My agent",
                              input_fields=[{"name": "question", "label": "Question", "default": ""}],
                              fork_fields=[ForkField("draft", "string", "Answer draft")])
    serve(executor, port=8020)                                       # then: Studio -> Connected agents -> attach

Depends only on LangGraph (and uvicorn for ``serve``). Does not import Agent Studio.
"""
from .core import PROTOCOL_VERSION, ExecutorConflict, ExecutorNotFound, ForkField, StudioExecutor, jsonable
from .server import TOKEN_HEADER, create_asgi_app, serve, serve_async

__all__ = ["PROTOCOL_VERSION", "TOKEN_HEADER", "ExecutorConflict", "ExecutorNotFound", "ForkField", "StudioExecutor",
           "create_asgi_app", "jsonable", "serve", "serve_async"]
