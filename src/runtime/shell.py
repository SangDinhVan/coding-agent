"""Conservative facts for shell commands with a known executable and input scope."""

import shlex
from pathlib import PurePosixPath


def analyze_shell(command, cwd='.'):
    opaque = {'kind': 'opaque', 'analysis_complete': False, 'input_scope': 'workspace'}
    quote = None
    for char in command:
        if char in '\\\n\r\x00':
            return opaque
        if quote:
            if char == quote:
                quote = None
            elif quote == '"' and char in '$`':
                return opaque
        elif char in "\"'":
            quote = char
        elif char in '$`*?[]{}~;|&<>()':
            return opaque
    try:
        argv = shlex.split(command)
    except ValueError:
        return opaque
    if not argv or quote:
        return opaque
    executable = PurePosixPath(argv[0])
    if argv[0] not in {executable.name, '/bin/' + executable.name, '/usr/bin/' + executable.name}:
        return opaque
    name, args = executable.name, argv[1:]
    if (name in {'python', 'python3'} and args[:2] in (['-m', 'unittest'], ['-m', 'pytest'])) or name == 'pytest' or (name == 'npm' and args[:1] == ['test']):
        return {'kind': 'project_execution', 'analysis_complete': True,
                'executable': argv[0], 'argv': argv, 'input_scope': 'workspace'}

    paths = []
    if name == 'pwd':
        if any(arg not in {'-L', '-P'} for arg in args):
            return opaque
        paths = ['.']
    elif name == 'find':
        index = 0
        while index < len(args) and not args[index].startswith('-'):
            paths.append(args[index])
            index += 1
        paths = paths or ['.']
        while index < len(args):
            option = args[index]
            if option in {'-print', '-print0', '-empty'}:
                index += 1
                continue
            if option not in {'-maxdepth', '-mindepth', '-type', '-name', '-iname', '-path', '-ipath'} or index + 1 >= len(args):
                return opaque
            index += 2
    elif name in {'ls', 'cat', 'head', 'tail', 'grep'}:
        flags = {'ls': 'aAlhR1dFprStucUin', 'cat': 'AbensETv',
                 'head': 'qv', 'tail': 'qv', 'grep': 'EFGinvwxclLhHsqorRabI'}[name]
        long_flags = {'ls': {'--all', '--almost-all', '--human-readable', '--recursive'},
                      'cat': {'--number', '--number-nonblank', '--squeeze-blank'},
                      'head': {'--quiet', '--verbose'}, 'tail': {'--quiet', '--verbose'},
                      'grep': {'--line-number', '--ignore-case', '--fixed-strings',
                               '--extended-regexp', '--recursive', '--files-with-matches'}}[name]
        positional, index, options = [], 0, True
        while index < len(args):
            arg = args[index]
            if options and arg == '--':
                options = False
            elif options and name in {'head', 'tail'} and arg in {'-n', '-c', '--lines', '--bytes'}:
                index += 1
                if index >= len(args) or not args[index].lstrip('+-').isdigit():
                    return opaque
            elif options and arg.startswith('-') and arg != '-':
                if arg not in long_flags and not (not arg.startswith('--') and all(c in flags for c in arg[1:])):
                    return opaque
            else:
                positional.append(arg)
            index += 1
        if name == 'grep':
            if not positional:
                return opaque
            positional = positional[1:]
        paths = [path for path in positional if path != '-']
        if not paths and (name == 'ls' or (name == 'grep' and any(
                arg == '--recursive' or (arg.startswith('-') and not arg.startswith('--')
                                          and any(c in arg[1:] for c in 'rR')) for arg in args))):
            paths = ['.']
    else:
        return opaque

    valid = all(not PurePosixPath(path).is_absolute() and '..' not in PurePosixPath(path).parts
                and '\\' not in path for path in paths)
    return {'kind': 'static_readonly', 'analysis_complete': True, 'executable': argv[0],
            'argv': argv, 'input_scope': 'paths', 'scope_valid': valid,
            'reads_file_contents': name in {'cat', 'head', 'tail', 'grep'},
            'input_paths': [str(PurePosixPath(cwd) / path) for path in paths]}
