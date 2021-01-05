from asyncio import gather
from itertools import chain
from locale import strxfrm
from mimetypes import guess_type
from operator import add, sub
from os import linesep
from os.path import basename, dirname, exists, isdir, join, relpath, sep, splitext
from typing import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    cast,
)

from pynvim import Nvim
from pynvim.api.buffer import Buffer
from pynvim.api.common import NvimError
from pynvim.api.window import Window
from pynvim_pp.lib import async_call, write
from std2.asyncio import run_in_executor
from std2.types import Void

from .cartographer import new as new_root
from .da import human_readable_size
from .fs import (
    ancestors,
    copy,
    cut,
    fs_exists,
    fs_stat,
    is_parent,
    new,
    remove,
    rename,
    unify_ancestors,
)
from .git import status
from .localization import LANG
from .nvim import getcwd
from .opts import ArgparseError, parse_args
from .quickfix import quickfix
from .registry import autocmd, rpc
from .search import search
from .state import dump_session, forward
from .state import index as state_index
from .state import is_dir
from .system import SystemIntegrationError, open_gui, trash
from .types import (
    ClickType,
    FilterPattern,
    Index,
    Mode,
    Node,
    Selection,
    Settings,
    Stage,
    State,
    VCStatus,
)
from .wm import (
    find_current_buffer_name,
    is_fm_buffer,
    kill_buffers,
    kill_fm_windows,
    resize_fm_windows,
    show_file,
    toggle_fm_window,
    update_buffers,
)


def find_buffer(nvim: Nvim, bufnr: int) -> Optional[Buffer]:
    buffers: Sequence[Buffer] = nvim.api.list_bufs()
    for buffer in buffers:
        if buffer.number == bufnr:
            return buffer
    return None


async def _index(nvim: Nvim, state: State) -> Optional[Node]:
    def cont() -> Optional[Node]:
        window: Window = nvim.api.get_current_win()
        buffer: Buffer = nvim.api.win_get_buf(window)
        if is_fm_buffer(nvim, buffer=buffer):
            row, _ = nvim.api.win_get_cursor(window)
            row = row - 1
            return state_index(state, row)
        else:
            return None

    return await async_call(nvim, cont)


async def _indices(nvim: Nvim, state: State, is_visual: bool) -> Sequence[Node]:
    def step() -> Iterator[Node]:
        if is_visual:
            buffer: Buffer = nvim.api.get_current_buf()
            r1, _ = nvim.api.buf_get_mark(buffer, "<")
            r2, _ = nvim.api.buf_get_mark(buffer, ">")
            for row in range(r1 - 1, r2):
                node = state_index(state, row)
                if node:
                    yield node
        else:
            window: Window = nvim.api.get_current_win()
            row, _ = nvim.api.win_get_cursor(window)
            row = row - 1
            node = state_index(state, row)
            if node:
                yield node

    def cont() -> Sequence[Node]:
        return tuple(step())

    return await async_call(nvim, cont)


async def redraw(nvim: Nvim, state: State, focus: Optional[str]) -> None:
    def cont() -> None:
        update_buffers(nvim, state=state, focus=focus)

    await async_call(nvim, cont)


def _display_path(path: str, state: State) -> str:
    raw = relpath(path, start=state.root.path)
    name = raw.replace(linesep, r"\n")
    if isdir(path):
        return f"{name}{sep}"
    else:
        return name


async def _current(
    nvim: Nvim, state: State, settings: Settings, current: str
) -> Optional[Stage]:
    if is_parent(parent=state.root.path, child=current):
        paths: Set[str] = {*ancestors(current)} if state.follow else set()
        index = state.index | paths
        new_state = await forward(
            state, settings=settings, index=index, paths=paths, current=current
        )
        return Stage(new_state)
    else:
        return None


async def _change_dir(
    nvim: Nvim, state: State, settings: Settings, new_base: str
) -> Stage:
    index = state.index | {new_base}
    root = await new_root(new_base, index=index)
    new_state = await forward(state, settings=settings, root=root, index=index)
    return Stage(new_state)


async def _refocus(nvim: Nvim, state: State, settings: Settings) -> Stage:
    """
    Follow cwd update
    """

    cwd = await getcwd(nvim)
    return await _change_dir(nvim, state=state, settings=settings, new_base=cwd)


@rpc(blocking=False, name="CHADrefocus")
async def c_changedir(nvim: Nvim, state: State, settings: Settings) -> Stage:
    return await _refocus(nvim, state=state, settings=settings)


autocmd("DirChanged") << f"lua {c_changedir.remote_name}()"


@rpc(blocking=False)
async def a_follow(nvim: Nvim, state: State, settings: Settings) -> Optional[Stage]:
    """
    Follow buffer
    """

    def cont() -> str:
        name = find_current_buffer_name(nvim)
        return name

    current = await async_call(nvim, cont)
    if current:
        return await _current(nvim, state=state, settings=settings, current=current)
    else:
        return None


autocmd("BufEnter") << f"lua {a_follow.remote_name}()"


@rpc(blocking=False)
async def a_session(nvim: Nvim, state: State, settings: Settings) -> None:
    """
    Save CHADTree state
    """

    dump_session(state)


autocmd("FocusLost", "ExitPre") << f"lua {a_session.remote_name}()"


@rpc(blocking=False)
async def a_quickfix(nvim: Nvim, state: State, settings: Settings) -> Stage:
    """
    Update quickfix list
    """

    qf = await quickfix(nvim)
    new_state = await forward(state, settings=settings, qf=qf)
    return Stage(new_state)


autocmd("QuickfixCmdPost") << f"lua {a_quickfix.remote_name}()"


@rpc(blocking=False, name="CHADquit")
async def c_quit(nvim: Nvim, state: State, settings: Settings) -> None:
    """
    Close sidebar
    """

    def cont() -> None:
        kill_fm_windows(nvim, settings=settings)

    await async_call(nvim, cont)


@rpc(blocking=False)
async def c_open(
    nvim: Nvim, state: State, settings: Settings, args: Sequence[str]
) -> Optional[Stage]:
    """
    Toggle sidebar
    """

    try:
        opts = parse_args(args)
    except ArgparseError as e:
        await write(nvim, e, error=True)
        return None
    else:

        def cont() -> str:
            name = find_current_buffer_name(nvim)
            toggle_fm_window(nvim, state=state, settings=settings, opts=opts)
            return name

        current = await async_call(nvim, cont)

        stage = await _current(nvim, state=state, settings=settings, current=current)
        if stage:
            return stage
        else:
            return Stage(state)


#     @function("CHADtoggle_follow")
#     def toggle_follow(self, args: Sequence[Any]) -> None:
#         """
#         Toggle follow
#         """

#         self._run(c_toggle_follow)

#     @function("CHADtoggle_version_control")
#     def toggle_vc(self, args: Sequence[Any]) -> None:
#         """
#         Toggle version control
#         """

#         self._run(c_toggle_vc)


async def _resize(
    nvim: Nvim, state: State, settings: Settings, direction: Callable[[int, int], int]
) -> Stage:
    width = max(direction(state.width, 10), 1)
    new_state = await forward(state, settings=settings, width=width)

    def cont() -> None:
        resize_fm_windows(nvim, width=new_state.width)

    await async_call(nvim, cont)
    return Stage(new_state)


@rpc(blocking=False, name="CHADbigger")
async def c_bigger(nvim: Nvim, state: State, settings: Settings) -> Stage:
    """
    Bigger sidebar
    """
    return await _resize(nvim, state=state, settings=settings, direction=add)


@rpc(blocking=False, name="CHADsmaller")
async def c_smaller(nvim: Nvim, state: State, settings: Settings) -> Stage:
    """
    Smaller sidebar
    """
    return await _resize(nvim, state=state, settings=settings, direction=sub)


async def _open_file(
    nvim: Nvim, state: State, settings: Settings, path: str, click_type: ClickType
) -> Optional[Stage]:
    name = basename(path)
    _, ext = splitext(name)
    mime, _ = guess_type(name, strict=False)
    m_type, _, _ = (mime or "").partition("/")

    def ask() -> bool:
        question = LANG("mime_warn", name=name, mime=str(mime))
        resp: int = nvim.funcs.confirm(question, LANG("ask_yesno", linesep=linesep), 2)
        return resp == 1

    ans = (
        (await async_call(nvim, ask))
        if m_type in settings.mime.warn and ext not in settings.mime.ignore_exts
        else True
    )
    if ans:
        new_state = await forward(state, settings=settings, current=path)

        def cont() -> None:
            show_file(
                nvim,
                state=new_state,
                settings=settings,
                click_type=click_type,
            )

        await async_call(nvim, cont)
        return Stage(new_state)
    else:
        return None


async def _click(
    nvim: Nvim, state: State, settings: Settings, click_type: ClickType
) -> Optional[Stage]:
    node = await _index(nvim, state=state)

    if node:
        if Mode.orphan_link in node.mode:
            name = node.name
            await write(nvim, LANG("dead_link", name=name), error=True)
            return None
        else:
            if Mode.folder in node.mode:
                if state.filter_pattern:
                    await write(nvim, LANG("filter_click"))
                    return None
                else:
                    paths = {node.path}
                    index = state.index ^ paths
                    new_state = await forward(
                        state, settings=settings, index=index, paths=paths
                    )
                    return Stage(new_state)
            else:
                nxt = await _open_file(
                    nvim,
                    state=state,
                    settings=settings,
                    path=node.path,
                    click_type=click_type,
                )
                return nxt
    else:
        return None


@rpc(blocking=False, name="CHADprimary")
async def c_primary(nvim: Nvim, state: State, settings: Settings) -> Optional[Stage]:
    """
    Folders -> toggle
    File -> open
    """

    return await _click(
        nvim, state=state, settings=settings, click_type=ClickType.primary
    )


@rpc(blocking=False, name="CHADsecondary")
async def c_secondary(nvim: Nvim, state: State, settings: Settings) -> Optional[Stage]:
    """
    Folders -> toggle
    File -> preview
    """

    return await _click(
        nvim, state=state, settings=settings, click_type=ClickType.secondary
    )


@rpc(blocking=False, name="CHADtertiary")
async def c_tertiary(nvim: Nvim, state: State, settings: Settings) -> Optional[Stage]:
    """
    Folders -> toggle
    File -> open in new tab
    """

    return await _click(
        nvim, state=state, settings=settings, click_type=ClickType.tertiary
    )


@rpc(blocking=False, name="CHADv_split")
async def c_v_split(nvim: Nvim, state: State, settings: Settings) -> Optional[Stage]:
    """
    Folders -> toggle
    File -> open in vertical split
    """

    return await _click(
        nvim, state=state, settings=settings, click_type=ClickType.v_split
    )


@rpc(blocking=False, name="CHADh_split")
async def c_h_split(nvim: Nvim, state: State, settings: Settings) -> Optional[Stage]:
    """
    Folders -> toggle
    File -> open in horizontal split
    """

    return await _click(
        nvim, state=state, settings=settings, click_type=ClickType.h_split
    )


@rpc(blocking=False, name="CHADchange_focus")
async def c_change_focus(
    nvim: Nvim, state: State, settings: Settings
) -> Optional[Stage]:
    """
    Refocus root directory
    """

    node = await _index(nvim, state=state)
    if node:
        new_base = node.path if Mode.folder in node.mode else dirname(node.path)
        return await _change_dir(
            nvim, state=state, settings=settings, new_base=new_base
        )
    else:
        return None


@rpc(blocking=False, name="CHADchange_focus_up")
async def c_change_focus_up(
    nvim: Nvim, state: State, settings: Settings
) -> Optional[Stage]:
    """
    Refocus root directory up
    """

    c_root = state.root.path
    parent = dirname(c_root)
    if parent and parent != c_root:
        return await _change_dir(nvim, state=state, settings=settings, new_base=parent)
    else:
        return None


@rpc(blocking=False, name="CHADcollapse")
async def c_collapse(nvim: Nvim, state: State, settings: Settings) -> Optional[Stage]:
    """
    Collapse folder
    """

    node = await _index(nvim, state=state)
    if node:
        path = node.path if Mode.folder in node.mode else dirname(node.path)
        if path != state.root.path:
            paths = {
                i for i in state.index if i == path or is_parent(parent=path, child=i)
            }
            index = state.index - paths
            new_state = await forward(
                state, settings=settings, index=index, paths=paths
            )
            row = new_state.paths_lookup.get(path, 0)
            if row:

                def cont() -> None:
                    window: Window = nvim.api.get_current_win()
                    _, col = nvim.api.win_get_cursor(window)
                    nvim.api.win_set_cursor(window, (row + 1, col))

                await async_call(nvim, cont)

            return Stage(new_state)
        else:
            return None
    else:
        return None


async def _vc_stat(enable: bool) -> VCStatus:
    if enable:
        return await status()
    else:
        return VCStatus()


async def _refresh(
    nvim: Nvim, state: State, settings: Settings, write_out: bool = False
) -> Stage:
    """
    Redraw buffers
    """

    if write_out:
        await write(nvim, LANG("hourglass"))

    def co() -> str:
        current = find_current_buffer_name(nvim)
        return current

    current = await async_call(nvim, co)
    cwd = state.root.path
    paths = {cwd}
    new_current = current if is_parent(parent=cwd, child=current) else None

    def cont() -> Tuple[Index, Selection]:
        index = {i for i in state.index if exists(i)} | paths
        selection = (
            set() if state.filter_pattern else {s for s in state.selection if exists(s)}
        )
        return index, selection

    index, selection = await run_in_executor(cont)
    current_paths: Set[str] = {*ancestors(current)} if state.follow else set()
    new_index = index if new_current else index | current_paths

    qf, vc = await gather(quickfix(nvim), _vc_stat(state.enable_vc))
    new_state = await forward(
        state,
        settings=settings,
        index=new_index,
        selection=selection,
        qf=qf,
        vc=vc,
        paths=paths,
        current=new_current or Void,
    )

    if write_out:
        await write(nvim, LANG("ok_sym"))

    return Stage(new_state)


@rpc(blocking=False)
async def a_schedule_update(
    nvim: Nvim, state: State, settings: Settings
) -> Optional[Stage]:
    try:
        return await _refresh(nvim, state=state, settings=settings, write_out=False)
    except NvimError:
        return None


autocmd("BufWritePost", "FocusGained") << f"lua {a_schedule_update.remote_name}()"


@rpc(blocking=False, name="CHADrefresh")
async def c_refresh(nvim: Nvim, state: State, settings: Settings) -> Stage:
    return await _refresh(nvim, state=state, settings=settings, write_out=True)


@rpc(blocking=False, name="CHADjump_to_current")
async def c_jump_to_current(
    nvim: Nvim, state: State, settings: Settings
) -> Optional[Stage]:
    """
    Jump to active file
    """

    current = state.current
    if current:
        stage = await _current(nvim, state=state, settings=settings, current=current)
        if stage:
            return Stage(state=stage.state, focus=current)
        else:
            return None
    else:
        return None


@rpc(blocking=False, name="CHADtoggle_hidden")
async def c_hidden(nvim: Nvim, state: State, settings: Settings) -> Stage:
    """
    Toggle hidden
    """

    new_state = await forward(
        state, settings=settings, show_hidden=not state.show_hidden
    )
    return Stage(new_state)


@rpc(blocking=False)
async def c_toggle_follow(nvim: Nvim, state: State, settings: Settings) -> Stage:
    new_state = await forward(state, settings=settings, follow=not state.follow)
    await write(nvim, LANG("follow_mode_indi", follow=str(new_state.follow)))
    return Stage(new_state)


@rpc(blocking=False)
async def c_toggle_vc(nvim: Nvim, state: State, settings: Settings) -> Stage:
    enable_vc = not state.enable_vc
    vc = await _vc_stat(enable_vc)
    new_state = await forward(state, settings=settings, enable_vc=enable_vc, vc=vc)
    await write(nvim, LANG("version_control_indi", enable_vc=str(new_state.enable_vc)))
    return Stage(new_state)


@rpc(blocking=False, name="CHADfilter")
async def c_new_filter(nvim: Nvim, state: State, settings: Settings) -> Stage:
    """
    Update filter
    """

    def ask() -> Optional[str]:
        pattern = state.filter_pattern.pattern if state.filter_pattern else ""
        resp: Optional[str] = nvim.funcs.input(LANG("new_filter"), pattern)
        return resp

    pattern = await async_call(nvim, ask)
    filter_pattern = FilterPattern(pattern=pattern) if pattern else None
    new_state = await forward(
        state, settings=settings, selection=set(), filter_pattern=filter_pattern
    )
    return Stage(new_state)


@rpc(blocking=False, name="CHADsearch")
async def c_new_search(nvim: Nvim, state: State, settings: Settings) -> Stage:
    """
    New search params
    """

    def ask() -> Optional[str]:
        pattern = ""
        resp: Optional[str] = nvim.funcs.input("new_search", pattern)
        return resp

    cwd = state.root.path
    pattern = await async_call(nvim, ask)
    results = await search(pattern or "", cwd=cwd, sep=linesep)
    await write(nvim, results)

    return Stage(state)


@rpc(blocking=False, name="CHADcopy_name")
async def c_copy_name(
    nvim: Nvim, state: State, settings: Settings, is_visual: bool
) -> None:
    """
    Copy dirname / filename
    """

    async def gen_paths() -> AsyncIterator[str]:
        selection = state.selection
        if is_visual or not selection:
            nodes = await _indices(nvim, state=state, is_visual=is_visual)
            for node in nodes:
                yield node.path
        else:
            for selected in sorted(selection, key=strxfrm):
                yield selected

    paths = [path async for path in gen_paths()]

    clip = linesep.join(paths)
    copied_paths = ", ".join(paths)

    def cont() -> None:
        nvim.funcs.setreg("+", clip)
        nvim.funcs.setreg("*", clip)

    await async_call(nvim, cont)
    await write(nvim, LANG("copy_paths", copied_paths=copied_paths))


@rpc(blocking=False, name="CHADstat")
async def c_stat(nvim: Nvim, state: State, settings: Settings) -> None:
    """
    Print file stat to cmdline
    """

    node = await _index(nvim, state=state)
    if node:
        try:
            stat = await fs_stat(node.path)
        except Exception as e:
            await write(nvim, e, error=True)
        else:
            permissions = stat.permissions
            size = human_readable_size(stat.size, truncate=2)
            user = stat.user
            group = stat.group
            mtime = format(stat.date_mod, settings.view.time_fmt)
            name = node.name + sep if Mode.folder in node.mode else node.name
            full_name = f"{name} -> {stat.link}" if stat.link else name
            mode_line = f"{permissions} {size} {user} {group} {mtime} {full_name}"
            await write(nvim, mode_line)


@rpc(blocking=False, name="CHADnew")
async def c_new(nvim: Nvim, state: State, settings: Settings) -> Optional[Stage]:
    """
    new file / folder
    """

    node = await _index(nvim, state=state) or state.root
    parent = node.path if is_dir(node) else dirname(node.path)

    def ask() -> Optional[str]:
        resp: Optional[str] = nvim.funcs.input(LANG("pencil"))
        return resp

    child = await async_call(nvim, ask)

    if child:
        path = join(parent, child)
        if await fs_exists(path):
            await write(nvim, LANG("already_exists", name=path), error=True)
            return Stage(state)
        else:
            try:
                await new(path)
            except Exception as e:
                await write(nvim, e, error=True)
                return await _refresh(nvim, state=state, settings=settings)
            else:
                paths = {*ancestors(path)}
                index = state.index | paths
                new_state = await forward(
                    state, settings=settings, index=index, paths=paths
                )
                nxt = await _open_file(
                    nvim,
                    state=new_state,
                    settings=settings,
                    path=path,
                    click_type=ClickType.secondary,
                )
                return nxt
    else:
        return None


@rpc(blocking=False, name="CHADrename")
async def c_rename(nvim: Nvim, state: State, settings: Settings) -> Optional[Stage]:
    """
    rename file / folder
    """

    node = await _index(nvim, state=state)
    if node:
        prev_name = node.path
        parent = state.root.path
        rel_path = relpath(prev_name, start=parent)

        def ask() -> Optional[str]:
            resp: Optional[str] = nvim.funcs.input(LANG("pencil"), rel_path)
            return resp

        child = await async_call(nvim, ask)
        if child:
            new_name = join(parent, child)
            new_parent = dirname(new_name)
            if await fs_exists(new_name):
                await write(nvim, LANG("already_exists", name=new_name), error=True)
                return Stage(state)
            else:
                try:
                    await rename(prev_name, new_name)
                except Exception as e:
                    await write(nvim, e, error=True)
                    return await _refresh(nvim, state=state, settings=settings)
                else:
                    paths = {parent, new_parent, *ancestors(new_parent)}
                    index = state.index | paths
                    new_state = await forward(
                        state, settings=settings, index=index, paths=paths
                    )

                    def cont() -> None:
                        kill_buffers(nvim, paths=(prev_name,))

                    await async_call(nvim, cont)
                    return Stage(new_state)
        else:
            return None
    else:
        return None


@rpc(blocking=False, name="CHADclear_selection")
async def c_clear_selection(nvim: Nvim, state: State, settings: Settings) -> Stage:
    """
    Clear selected
    """

    new_state = await forward(state, settings=settings, selection=set())
    return Stage(new_state)


@rpc(blocking=False, name="CHADclear_filter")
async def c_clear_filter(nvim: Nvim, state: State, settings: Settings) -> Stage:
    """
    Clear filter
    """

    new_state = await forward(state, settings=settings, filter_pattern=None)
    return Stage(new_state)


@rpc(blocking=False, name="CHADselect")
async def c_select(
    nvim: Nvim, state: State, settings: Settings, is_visual: bool
) -> Optional[Stage]:
    """
    Folder / File -> select
    """

    nodes = iter(await _indices(nvim, state=state, is_visual=is_visual))
    if is_visual:
        selection = state.selection ^ {n.path for n in nodes}
        new_state = await forward(state, settings=settings, selection=selection)
        return Stage(new_state)
    else:
        node = next(nodes, None)
        if node:
            selection = state.selection ^ {node.path}
            new_state = await forward(state, settings=settings, selection=selection)
            return Stage(new_state)
        else:
            return None


async def _delete(
    nvim: Nvim,
    state: State,
    settings: Settings,
    is_visual: bool,
    yeet: Callable[[Iterable[str]], Awaitable[None]],
) -> Optional[Stage]:
    selection = state.selection or {
        node.path for node in await _indices(nvim, state=state, is_visual=is_visual)
    }
    unified = tuple(unify_ancestors(selection))
    if unified:
        display_paths = linesep.join(
            sorted((_display_path(path, state=state) for path in unified), key=strxfrm)
        )

        def ask() -> bool:
            question = LANG("ask_trash", linesep=linesep, display_paths=display_paths)
            resp: int = nvim.funcs.confirm(
                question, LANG("ask_yesno", linesep=linesep), 2
            )
            return resp == 1

        ans = await async_call(nvim, ask)
        if ans:
            try:
                await yeet(unified)
            except Exception as e:
                await write(nvim, e, error=True)
                return await _refresh(nvim, state=state, settings=settings)
            else:
                paths = {dirname(path) for path in unified}
                new_state = await forward(
                    state, settings=settings, selection=set(), paths=paths
                )

                def cont() -> None:
                    kill_buffers(nvim, paths=selection)

                await async_call(nvim, cont)
                return Stage(new_state)
        else:
            return None
    else:
        return None


@rpc(blocking=False, name="CHADdelete")
async def c_delete(
    nvim: Nvim, state: State, settings: Settings, is_visual: bool
) -> Optional[Stage]:
    """
    Delete selected
    """

    return await _delete(
        nvim, state=state, settings=settings, is_visual=is_visual, yeet=remove
    )


@rpc(blocking=False, name="CHADtrash")
async def c_trash(
    nvim: Nvim, state: State, settings: Settings, is_visual: bool
) -> Optional[Stage]:
    """
    Delete selected
    """

    return await _delete(
        nvim, state=state, settings=settings, is_visual=is_visual, yeet=trash
    )


def _find_dest(src: str, node: Node) -> str:
    name = basename(src)
    parent = node.path if is_dir(node) else dirname(node.path)
    dst = join(parent, name)
    return dst


async def _operation(
    nvim: Nvim,
    *,
    state: State,
    settings: Settings,
    op_name: str,
    action: Callable[[Mapping[str, str]], Awaitable[None]],
) -> Optional[Stage]:
    node = await _index(nvim, state=state)
    selection = state.selection
    unified = tuple(unify_ancestors(selection))
    if unified and node:

        def pre_op() -> MutableMapping[str, str]:
            op = {src: _find_dest(src, cast(Node, node)) for src in unified}
            return op

        operations = await run_in_executor(pre_op)

        def p_pre() -> Mapping[str, str]:
            pe = {s: d for s, d in operations.items() if exists(d)}
            return pe

        pre_existing = await run_in_executor(p_pre)

        if pre_existing:
            for source, dest in pre_existing.items():

                def ask_rename() -> Optional[str]:
                    resp: Optional[str] = nvim.funcs.input(
                        LANG("path_exists_err"), dest
                    )
                    return resp

                new_dest = await async_call(nvim, ask_rename)
                if new_dest:
                    operations[source] = new_dest
                else:
                    return None

            pre_existing = await run_in_executor(p_pre)

        if pre_existing:
            msg = ", ".join(
                f"{_display_path(s, state=state)} -> {_display_path(d, state=state)}"
                for s, d in sorted(pre_existing.items(), key=lambda t: strxfrm(t[0]))
            )
            await write(
                nvim, f"⚠️  -- {op_name}: path(s) already exist! :: {msg}", error=True
            )
            return None
        else:
            msg = linesep.join(
                f"{_display_path(s, state=state)} -> {_display_path(d, state=state)}"
                for s, d in sorted(operations.items(), key=lambda t: strxfrm(t[0]))
            )

            def ask() -> bool:
                question = f"{op_name}{linesep}{msg}?"
                resp: int = nvim.funcs.confirm(
                    question, LANG("ask_yesno", linesep=linesep), 2
                )
                return resp == 1

            ans = await async_call(nvim, ask)
            if ans:
                try:
                    await action(operations)
                except Exception as e:
                    await write(nvim, e, error=True)
                    return await _refresh(nvim, state=state, settings=settings)
                else:
                    paths = {
                        dirname(p)
                        for p in chain(operations.keys(), operations.values())
                    }
                    index = state.index | paths
                    new_state = await forward(
                        state,
                        settings=settings,
                        index=index,
                        selection=set(),
                        paths=paths,
                    )

                    def cont() -> None:
                        kill_buffers(nvim, paths=selection)

                    await async_call(nvim, cont)
                    return Stage(new_state)
            else:
                return None
    else:
        await write(nvim, LANG("nothing_select"), error=True)
        return None


@rpc(blocking=False, name="CHADcut")
async def c_cut(nvim: Nvim, state: State, settings: Settings) -> Optional[Stage]:
    """
    Cut selected
    """

    return await _operation(
        nvim, state=state, settings=settings, op_name=LANG("cut"), action=cut
    )


@rpc(blocking=False, name="CHADcopy")
async def c_copy(nvim: Nvim, state: State, settings: Settings) -> Optional[Stage]:
    """
    Copy selected
    """

    return await _operation(
        nvim, state=state, settings=settings, op_name=LANG("copy"), action=copy
    )


@rpc(blocking=False, name="CHADopen_sys")
async def c_open_system(nvim: Nvim, state: State, settings: Settings) -> None:
    """
    Open using finder / dolphin, etc
    """

    node = await _index(nvim, state=state)
    if node:
        try:
            await open_gui(node.path)
        except SystemIntegrationError as e:
            await write(nvim, e)


#         groups = chain(
#             self.settings.hl_context.groups,
#             self.settings.icons.colours.exts.values(),
#         )
# highlight(*groups)
#         await add_hl_groups(self.nvim, groups=groups)
