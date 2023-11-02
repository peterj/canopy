import os
import logging
import signal
import sys
import uuid

import openai
from multiprocessing import current_process

import yaml
from dotenv import load_dotenv

from canopy.llm import BaseLLM
from canopy.tokenizer import Tokenizer
from canopy.knowledge_base import KnowledgeBase
from canopy.context_engine import ContextEngine
from canopy.chat_engine import ChatEngine
from starlette.concurrency import run_in_threadpool
from sse_starlette.sse import EventSourceResponse

from fastapi import FastAPI, HTTPException, Body
import uvicorn
from typing import cast, Union

from canopy.models.api_models import (
    StreamingChatResponse,
    ChatResponse,
)
from canopy.models.data_models import Context, UserMessage, ContextContentResponse
from .api_models import (
    ChatRequest,
    ContextQueryRequest,
    ContextUpsertRequest,
    HealthStatus,
    ContextDeleteRequest,
    ShutdownResponse,
    SuccessUpsertResponse,
    SuccessDeleteResponse,
)

from canopy.llm.openai import OpenAILLM
from canopy_cli.errors import ConfigError
from canopy_server import description
from canopy import __version__


APIChatResponse = Union[ChatResponse, EventSourceResponse]

load_dotenv()  # load env vars before import of openai
openai.api_key = os.getenv("OPENAI_API_KEY")

app = FastAPI(
    title="Canopy API",
    description=description,
    version=__version__,
    license_info={
        "name": "Apache 2.0",
        "url": "https://www.apache.org/licenses/LICENSE-2.0.html",
    },
)

context_engine: ContextEngine
chat_engine: ChatEngine
kb: KnowledgeBase
llm: BaseLLM
logger: logging.Logger


@app.post(
    "/context/chat/completions",
    response_model=APIChatResponse,
    responses={500: {"description": "Failed to chat with Canopy"}},  # noqa: E501
)
async def chat(
    request: ChatRequest = Body(...),
) -> APIChatResponse:
    """
    Chat with Canopy, using the LLM and context engine, and return a response.

    The request schema is following OpenAI's chat completion API schema, but removes the need to configure
    anything, other than the messages field: for more imformation see: https://platform.openai.com/docs/api-reference/chat/create

    """  # noqa: E501
    try:
        session_id = request.user or "None"  # noqa: F841
        question_id = str(uuid.uuid4())
        logger.debug(f"Received chat request: {request.messages[-1].content}")
        answer = await run_in_threadpool(
            chat_engine.chat, messages=request.messages, stream=request.stream
        )

        if request.stream:

            def stringify_content(response: StreamingChatResponse):
                for chunk in response.chunks:
                    chunk.id = question_id
                    data = chunk.json()
                    yield data

            content_stream = stringify_content(cast(StreamingChatResponse, answer))
            return EventSourceResponse(content_stream, media_type="text/event-stream")

        else:
            chat_response = cast(ChatResponse, answer)
            chat_response.id = question_id
            return chat_response

    except Exception as e:
        logger.exception(f"Chat with question_id {question_id} failed")
        raise HTTPException(status_code=500, detail=f"Internal Service Error: {str(e)}")


@app.post(
    "/context/query",
    response_model=ContextContentResponse,
    responses={
        500: {"description": "Failed to query the knowledgebase or Build the context"}
    },
)
async def query(
    request: ContextQueryRequest = Body(...),
) -> ContextContentResponse:
    """
    Query the knowledgebase and return a context. Context is a collections of text snippets, each with a source.
    Query enables tuning the context length (in tokens) such that you can cap the cost of the generation.
    This method can be used with or without a LLM.
    """  # noqa: E501
    try:
        context: Context = await run_in_threadpool(
            context_engine.query,
            queries=request.queries,
            max_context_tokens=request.max_tokens,
        )

        return context.content

    except Exception as e:
        logger.exception(e)
        raise HTTPException(status_code=500, detail=f"Internal Service Error: {str(e)}")


@app.post(
    "/context/upsert",
    response_model=SuccessUpsertResponse,
    responses={500: {"description": "Failed to upsert documents"}},
)
async def upsert(
    request: ContextUpsertRequest = Body(...),
) -> SuccessUpsertResponse:
    """
    Upsert documents into the knowledgebase. Upserting is a way to add new documents or update existing ones.
    Each document has a unique ID. If a document with the same ID already exists, it will be updated.

    This method will run the processing, chunking and endocing of the data in parallel, and then send the
    encoded data to the Pinecone Index in batches.
    """  # noqa: E501
    try:
        logger.info(f"Upserting {len(request.documents)} documents")
        await run_in_threadpool(
            kb.upsert, documents=request.documents, batch_size=request.batch_size
        )

        return SuccessUpsertResponse()

    except Exception as e:
        logger.exception(e)
        raise HTTPException(status_code=500, detail=f"Internal Service Error: {str(e)}")


@app.post(
    "/context/delete",
    response_model=SuccessDeleteResponse,
    responses={500: {"description": "Failed to delete documents"}},
)
async def delete(
    request: ContextDeleteRequest = Body(...),
) -> SuccessDeleteResponse:
    """
    Delete documents from the knowledgebase. Deleting documents is done by their unique ID.
    """  # noqa: E501
    try:
        logger.info(f"Delete {len(request.document_ids)} documents")
        await run_in_threadpool(kb.delete, document_ids=request.document_ids)
        return SuccessDeleteResponse()

    except Exception as e:
        logger.exception(e)
        raise HTTPException(status_code=500, detail=f"Internal Service Error: {str(e)}")


@app.get(
    "/health",
    response_model=HealthStatus,
    responses={500: {"description": "Failed to connect to Pinecone or LLM"}},
)
@app.exception_handler(Exception)
async def health_check() -> HealthStatus:
    """
    Health check for the Canopy server. This endpoint checks the connection to Pinecone and the LLM.
    """  # noqa: E501
    try:
        await run_in_threadpool(kb.verify_index_connection)
    except Exception as e:
        err_msg = f"Failed connecting to Pinecone Index {kb._index_name}"
        logger.exception(err_msg)
        raise HTTPException(
            status_code=500, detail=f"{err_msg}. Error: {str(e)}"
        ) from e

    try:
        msg = UserMessage(content="This is a health check. Are you alive? Be concise")
        await run_in_threadpool(llm.chat_completion, messages=[msg], max_tokens=50)
    except Exception as e:
        err_msg = f"Failed to communicate with {llm.__class__.__name__}"
        logger.exception(err_msg)
        raise HTTPException(
            status_code=500, detail=f"{err_msg}. Error: {str(e)}"
        ) from e

    return HealthStatus(pinecone_status="OK", llm_status="OK")


@app.get("/shutdown")
async def shutdown() -> ShutdownResponse:
    """
    __WARNING__: Experimental method.


    This method will shutdown the server. It is used for testing purposes, and not recommended to be used
    in production.
    This method will locate the parent process and send a SIGINT signal to it.
    """  # noqa: E501
    logger.info("Shutting down")
    proc = current_process()
    pid = proc._parent_pid if "SpawnProcess" in proc.name else proc.pid
    os.kill(pid, signal.SIGINT)
    return ShutdownResponse()


@app.on_event("startup")
async def startup():
    _init_logging()
    _init_engines()


def _init_logging():
    global logger

    file_handler = logging.FileHandler(
        filename=os.getenv("CE_LOG_FILENAME", "canopy.log")
    )
    stdout_handler = logging.StreamHandler(stream=sys.stdout)
    handlers = [file_handler, stdout_handler]
    logging.basicConfig(
        format="%(asctime)s - %(processName)s - %(name)-10s [%(levelname)-8s]:  "
        "%(message)s",
        level=os.getenv("CE_LOG_LEVEL", "INFO").upper(),
        handlers=handlers,
        force=True,
    )
    logger = logging.getLogger(__name__)


def _init_engines():
    global kb, context_engine, chat_engine, llm, logger

    index_name = os.getenv("INDEX_NAME")
    if not index_name:
        raise ValueError("INDEX_NAME environment variable must be set")

    config_file = os.getenv("CANOPY_CONFIG_FILE")
    if config_file:
        _load_config(config_file)

    else:
        logger.info(
            "Did not find config file. Initializing engines with default "
            "configuration"
        )
        Tokenizer.initialize()
        kb = KnowledgeBase(index_name=index_name)
        context_engine = ContextEngine(knowledge_base=kb)
        llm = OpenAILLM()
        chat_engine = ChatEngine(context_engine=context_engine, llm=llm)

    kb.connect()


def _load_config(config_file):
    global chat_engine, llm, context_engine, kb, logger
    logger.info(f"Initializing engines with config file {config_file}")
    try:
        with open(config_file, "r") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        logger.exception(f"Failed to load config file {config_file}")
        raise ConfigError(f"Failed to load config file {config_file}. Error: {str(e)}")
    tokenizer_config = config.get("tokenizer", {})
    Tokenizer.initialize_from_config(tokenizer_config)
    if "chat_engine" not in config:
        raise ConfigError(
            f"Config file {config_file} must contain a 'chat_engine' section"
        )
    chat_engine_config = config["chat_engine"]
    try:
        chat_engine = ChatEngine.from_config(chat_engine_config)
    except Exception as e:
        logger.exception(
            f"Failed to initialize chat engine from config file {config_file}"
        )
        raise ConfigError(
            f"Failed to initialize chat engine from config file {config_file}."
            f" Error: {str(e)}"
        )
    llm = chat_engine.llm
    context_engine = chat_engine.context_engine
    kb = context_engine.knowledge_base


def start(host="0.0.0.0", port=8000, reload=False, config_file=None):
    if config_file:
        os.environ["CANOPY_CONFIG_FILE"] = config_file

    uvicorn.run("canopy_server.app:app", host=host, port=port, reload=reload, workers=0)


if __name__ == "__main__":
    start()
