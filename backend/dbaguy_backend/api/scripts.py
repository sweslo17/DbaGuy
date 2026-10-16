from fastapi import APIRouter, Depends, Response

from ..container import AppContainer
from ..domain.scripts import ScriptFile, ScriptFolder, ScriptRename, ScriptTree, ScriptWrite
from .deps import get_container, require_token

router = APIRouter(prefix="/scripts", tags=["scripts"], dependencies=[Depends(require_token)])


@router.get("", response_model=ScriptTree)
def script_tree(container: AppContainer = Depends(get_container)) -> ScriptTree:
    return container.scripts.tree()


@router.get("/file", response_model=ScriptFile)
def read_script(path: str, container: AppContainer = Depends(get_container)) -> ScriptFile:
    return container.scripts.read(path)


@router.put("/file", response_model=ScriptFile)
def write_script(body: ScriptWrite, container: AppContainer = Depends(get_container)) -> ScriptFile:
    return container.scripts.write(body.path, body.content)


@router.delete("/file", status_code=204)
def delete_script(path: str, container: AppContainer = Depends(get_container)) -> Response:
    container.scripts.delete(path)
    return Response(status_code=204)


@router.post("/rename", status_code=204)
def rename_script(body: ScriptRename, container: AppContainer = Depends(get_container)) -> Response:
    container.scripts.rename(body.source, body.target)
    return Response(status_code=204)


@router.post("/folder", status_code=204)
def create_script_folder(body: ScriptFolder, container: AppContainer = Depends(get_container)) -> Response:
    container.scripts.create_folder(body.path)
    return Response(status_code=204)
