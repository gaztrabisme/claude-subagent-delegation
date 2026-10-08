"""Shell path operands, retaining the distinction between quoting and expansion.

This is a metadata parser, never a shell evaluator. It only supplies credential
checks; the main classifier still decides whether the command may run.
"""

from __future__ import annotations

import ctypes
import glob
import os
import re
import stat
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import cache
from pathlib import Path

_META = '{},[]*?<>$`~'
_LITERAL = {char: '\x00' + char for char in _META}
_OPERATORS = '();<>|&\n'


@dataclass(frozen=True)
class Word:
    value: str
    pattern: str
    source: str

    def tail(self, start: int) -> Word:
        offset = self._offset(start)
        return Word(self.value[start:], self.pattern[offset:], self.source)

    def prefix(self, end: int) -> Word:
        return Word(self.value[:end], self.pattern[:self._offset(end)], self.source)

    def _offset(self, position: int) -> int:
        index = 0
        for _ in range(position):
            index += 2 if self.pattern[index:index + 1] == '\x00' else 1
        return index


def words(command: str) -> list[Word]:
    """Parse lexical shell words and operators without discarding quote state.

    Numeric zsh globs are a word component, rather than I/O redirections. An
    unmatched quote raises; the existing classifier handles the parse error.
    """
    output: list[Word] = []
    if '\x00' in command:
        raise ValueError('NUL is not valid in a shell argument')
    index = 0
    while index < len(command):
        if command[index] in ' \t\r':
            index += 1
            continue
        start = index
        if command[index] in _OPERATORS:
            number = re.match(r'<\d*-\d*>', command[index:])
            if not number:
                index += 1
                while index < len(command) and command[index] in _OPERATORS:
                    index += 1
                raw = command[start:index]
                output.append(Word(raw, raw, raw))
                continue
        value: list[str] = []
        pattern: list[str] = []
        quote = ''
        while index < len(command):
            char = command[index]
            if not quote and (char in ' \t\r' or char in _OPERATORS):
                number = re.match(r'<\d*-\d*>', command[index:]) if char == '<' else None
                if number:
                    value.append(number[0])
                    pattern.append(number[0])
                    index += len(number[0])
                    continue
                break
            if char in "'\"" and (not quote or char == quote):
                quote = '' if quote else char
                index += 1
                continue
            if char == '\\' and quote != "'" and index + 1 < len(command):
                following = command[index + 1]
                if not quote or following in '$`"\\\n':
                    if following != '\n':
                        value.append(following)
                        pattern.append(_LITERAL.get(following, following))
                    index += 2
                    continue
            value.append(char)
            literal = quote == "'" or (quote == '"' and char not in '$`')
            pattern.append(_LITERAL.get(char, char) if literal else char)
            index += 1
        if quote:
            raise ValueError('unmatched shell quote')
        output.append(Word(''.join(value), ''.join(pattern), command[start:index]))
    return output


def literal_pattern(pattern: str) -> str:
    """Restore quoted characters while escaping filename-glob metacharacters."""
    return re.sub('\x00(.)', lambda match: glob.escape(match[1]), pattern, flags=re.DOTALL)


def literal_value(pattern: str) -> str:
    return re.sub('\x00(.)', lambda match: match[1], pattern, flags=re.DOTALL)


def quote_pattern(value: str) -> str:
    return ''.join(_LITERAL.get(char, char) for char in value)


def active_pattern(pattern: str) -> str:
    return re.sub('\x00.', '', pattern, flags=re.DOTALL)


def first_magic(pattern: str, characters: str) -> int:
    index = 0
    while index < len(pattern):
        if pattern[index] == '\x00':
            index += 2
            continue
        if pattern[index] in characters:
            return index
        index += 1
    return len(pattern)


def brace_patterns(pattern: str, limit: int = 256) -> tuple[list[str], bool]:
    """Bounded Bash/zsh comma and range braces, including nested alternatives."""
    pending = [pattern]
    result: list[str] = []
    while pending:
        current = pending.pop()
        expansion = None
        stack: list[int] = []
        for index, char in enumerate(current):
            if char == '\x00' or (index and current[index - 1] == '\x00'):
                continue
            if char == '{':
                stack.append(index)
            elif char == '}' and stack:
                start = stack.pop()
                content = current[start + 1:index]
                depth = 0
                alternatives = []
                previous = 0
                for position, inner in enumerate(content):
                    if inner == '\x00' or (position and content[position - 1] == '\x00'):
                        continue
                    depth += (inner == '{') - (inner == '}')
                    if inner == ',' and depth == 0:
                        alternatives.append(content[previous:position])
                        previous = position + 1
                if alternatives:
                    alternatives.append(content[previous:])
                else:
                    numeric = re.fullmatch(r'(-?\d+)\.\.(-?\d+)(?:\.\.(-?\d+))?', content)
                    alphabetic = re.fullmatch(r'([a-zA-Z])\.\.([a-zA-Z])', content)
                    if numeric or alphabetic:
                        match = numeric or alphabetic
                        assert match is not None
                        first, last = ((int(match[1]), int(match[2])) if numeric else
                                       (ord(match[1]), ord(match[2])))
                        step = abs(int(match[3] or 1)) if numeric else 1
                        step = step if last >= first else -step
                        count = abs(last - first) // abs(step) + 1 if step else limit + 1
                        if count > limit or not step:
                            return result, True
                        stop = last + (1 if step > 0 else -1)
                        width = max(len(match[1]), len(match[2])) if numeric and any(
                            len(raw.lstrip('-')) > 1 and raw.lstrip('-').startswith('0')
                            for raw in (match[1], match[2])
                        ) else 0
                        alternatives = [(f'{number:0{width}d}' if numeric else chr(number))
                                        for number in range(first, stop, step)]
                if alternatives:
                    expansion = [current[:start] + value + current[index + 1:]
                                 for value in alternatives]
                    break
        if expansion:
            pending.extend(expansion)
        else:
            result.append(current)
        if len(pending) + len(result) > limit:
            return result, True
    return result, False


def glob_pattern(pattern: str) -> str:
    """Numeric range over-approximation; libc handles locale bracket classes."""
    pattern = re.sub(r'(?<!\x00)\[\^', '[!', pattern)
    # Range validation is done on matched names by numeric_matches below.
    pattern = re.sub(r'(?<!\x00)<\d*-\d*>', '[0-9]*', pattern)
    return literal_pattern(pattern)


try:
    _fnmatch = ctypes.CDLL(None).fnmatch
    _fnmatch.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    _fnmatch.restype = ctypes.c_int
except AttributeError:  # Platforms without POSIX libc, such as Windows.
    _fnmatch = None


def filename_matches(pattern: str, name: str) -> bool:
    if _fnmatch is not None:
        return _fnmatch(os.fsencode(pattern), os.fsencode(name), 0) == 0
    # Do not clear unsupported POSIX classes against only an ASCII subset.
    widened = re.sub(r'\[(?:\[:\w+:\]|[^\]])+\]', '?', pattern)
    return fnmatchcase(name, widened)


def numeric_matches(pattern: str, candidate: str) -> bool:
    ranges = list(re.finditer(r'(?<!\x00)<(\d*)-(\d*)>', pattern))
    if not ranges:
        return True
    # A neighboring wildcard may consume some digits: <1-3>* matches 21.
    # Match actual digit spans rather than a greedy regex capture or expanding
    # every integer in the range. Memoization bounds repeated partitions by
    # the number of ranges and the existing candidate's pathname length.
    portions = []
    previous = 0
    for match in ranges:
        portions.append(literal_pattern(pattern[previous:match.start()]))
        previous = match.end()
    suffix = literal_pattern(pattern[previous:])

    @cache
    def accepts(index: int, offset: int) -> bool:
        if index == len(ranges):
            return filename_matches(suffix, candidate[offset:])
        match = ranges[index]
        low = int(match[1]) if match[1] else None
        high = int(match[2]) if match[2] else None
        for start in range(offset, len(candidate)):
            if candidate[start] not in '0123456789' or not filename_matches(
                portions[index], candidate[offset:start],
            ):
                continue
            end = start
            while end < len(candidate) and candidate[end] in '0123456789':
                end += 1
                number = int(candidate[start:end])
                if ((low is None or number >= low) and (high is None or number <= high)
                        and accepts(index + 1, end)):
                    return True
        return False

    return accepts(0, 0)


def substitutions(command: str) -> list[str]:
    """Executed command substitutions, excluding single-quoted display text."""
    result = []
    quote = ''
    index = 0
    while index < len(command):
        char = command[index]
        if char == '\\' and quote != "'":
            index += 2
            continue
        if char in "'\"" and (not quote or quote == char):
            quote = '' if quote else char
            index += 1
            continue
        command_substitution = quote != "'" and command.startswith('$(', index)
        process_substitution = not quote and command[index:index + 2] in {'<(', '>('}
        if command_substitution or process_substitution:
            start = index + 2
            index = start
            depth = 1
            inner_quote = ''
            while index < len(command) and depth:
                inner = command[index]
                if inner == '\\' and inner_quote != "'":
                    index += 2
                    continue
                if inner in "'\"" and (not inner_quote or inner_quote == inner):
                    inner_quote = '' if inner_quote else inner
                elif not inner_quote:
                    depth += (inner == '(') - (inner == ')')
                index += 1
            if not depth:
                result.append(command[start:index - 1])
            continue
        if quote != "'" and char == '`':
            start = index + 1
            index = start
            while index < len(command) and command[index] != '`':
                index += 2 if command[index] == '\\' else 1
            if index < len(command):
                result.append(command[start:index])
                index += 1
            continue
        index += 1
    return result


def bounded_glob(pattern: str, limit: int = 16384) -> tuple[list[Path], bool]:
    """Expand file patterns with a metadata budget and a directory-cycle fence.

    Unlike a generator bounded only by its matches, this bounds traversal even
    when a recursive pattern has no matches. Hidden names are included so a
    credential cannot hide behind a shell's configurable dotglob setting.
    """
    path = Path(pattern)
    components = path.parts[1:] if path.is_absolute() else path.parts
    pending = [(Path(path.anchor or '.'), 0)]
    seen: set[tuple[int, int, int]] = set()
    matches: list[Path] = []
    entries = 0
    while pending:
        root, position = pending.pop()
        if position == len(components):
            if numeric_matches(pattern, str(root)):
                matches.append(root)
            continue
        raw_component = components[position]
        component = glob_pattern(raw_component)
        if not any(char in active_pattern(raw_component) for char in '*?[<'):
            candidate = root / literal_value(raw_component)
            if candidate.exists() or candidate.is_symlink():
                pending.append((candidate, position + 1))
            continue
        try:
            info = root.stat()
            identity = (info.st_dev, info.st_ino, position)
            if not stat.S_ISDIR(info.st_mode) or identity in seen:
                continue
            seen.add(identity)
            if component == '**':
                pending.append((root, position + 1))
            with os.scandir(root) as children:
                for child in children:
                    entries += 1
                    if entries > limit:
                        return matches, True
                    if component == '**':
                        try:
                            directory = child.is_dir(follow_symlinks=True)
                        except OSError:
                            return matches, True
                        if directory:
                            pending.append((Path(child.path), position))
                        if position == len(components) - 1:
                            pending.append((Path(child.path), position + 1))
                    elif filename_matches(component, child.name):
                        candidate = Path(child.path)
                        if position == len(components) - 1:
                            if numeric_matches(pattern, str(candidate)):
                                matches.append(candidate)
                        else:
                            pending.append((candidate, position + 1))
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError):
            return matches, True
    return matches, False


_SEARCH_VALUES = {'-e', '--regexp', '-g', '--glob', '--iglob', '-t', '--type', '-T',
                  '--type-not', '-A', '-B', '-C', '-m', '--max-count', '--max-depth',
                  '--max-columns', '--encoding', '-d', '--directories'}
_SEARCH_PATHS = {'-f', '--file', '--ignore-file'}
_PATH_OPTIONS = {
    'sort': {'-o', '--output', '-T', '--temporary-directory'},
    'diff': {'--exclude-from'},
    'git': {'-C', '--git-dir', '--work-tree', '--output', '--contents'},
    'pytest': {'-c', '--confcutdir', '--rootdir', '--basetemp'},
    'tox': {'-c', '--conf'}, 'nose2': {'-c', '--config', '-s', '--start-dir'},
    'unittest': {'-s', '--start-directory', '-t', '--top-level-directory'},
    'cargo': {'--manifest-path', '--target-dir'},
    'vitest': {'-c', '--config', '--root', '--dir'},
    'tsc': {'--project', '-p', '--outDir', '--outFile'},
    'cp': {'-t', '--target-directory'}, 'mv': {'-t', '--target-directory'},
    'curl': {'-o', '--output', '-K', '--config', '--cacert', '--cert', '--key'},
}
_DATA_OPTIONS = {
    'pytest': {'-k', '-m', '-o', '--override-ini', '--maxfail', '--tb', '--durations',
               '--capture', '--color', '-r', '--ignore-glob'},
    'cargo': {'--test', '--bin', '--example', '--bench', '--package', '-p', '--target',
              '--message-format', '--features', '-F'},
    'go': {'-run', '-bench', '-count', '-timeout', '-parallel'},
    'ls': {'-I', '--ignore', '--hide', '--format', '--sort', '--color', '--quoting-style',
           '--time', '--time-style', '--block-size', '-w', '--width'},
    'git': {'-G', '-S', '--grep', '--author', '--format', '--pretty'},
    'diff': {'-I', '--ignore-matching-lines', '-L', '--label', '-x', '--exclude'},
    'head': {'-n', '--lines', '-c', '--bytes'}, 'tail': {'-n', '--lines', '-c', '--bytes'},
    'sort': {'-k', '--key', '-t', '--field-separator', '-S', '--buffer-size'},
    'cut': {'-b', '--bytes', '-c', '--characters', '-f', '--fields', '-d', '--delimiter',
            '--output-delimiter'},
    'uniq': {'-f', '--skip-fields', '-s', '--skip-chars', '-w', '--check-chars'},
    'stat': {'-f', '-c', '--format', '--printf'},
    'du': {'-d', '--max-depth', '--block-size'},
}
_SEARCH = {'rg', 'ag', 'grep', 'egrep', 'fgrep'}
_DATA_COMMANDS = {'echo', 'printf', 'true', 'false', 'pwd', 'date'}
_TREE_COMMANDS = {'cp', 'mv', 'rm', 'rmdir', 'chmod', 'chown', 'shred', 'unlink', 'diff'}
_CURL_DATA = {'--data', '--data-binary', '--data-raw', '--data-urlencode', '-d', '--form', '-F'}


def _curl_file(word: Word, flag: str) -> Word | None:
    if flag in {'--data', '--data-binary', '-d'} and word.value.startswith('@'):
        return word.tail(1)
    if flag == '--data-urlencode' and '@' in word.value:
        index = word.value.index('@')
        if '=' not in word.value[:index]:
            return word.tail(index + 1)
    if flag in {'--form', '-F'} and '=' in word.value:
        index = word.value.index('=') + 1
        if word.value[index:index + 1] in {'@', '<'}:
            return word.tail(index + 1)
    return None


def operands(command: str) -> tuple[list[tuple[Word, bool]], list[Word]]:
    """Credential-bearing operands and recursive search roots.

    Pattern flags and selector/JSON/text arguments are data. Unknown options on
    recursive search also retain its default cwd root, as a fail-closed fallback.
    """
    tokens = words(command)
    paths: list[tuple[Word, bool]] = []
    roots: list[Word] = []
    segments: list[list[Word]] = [[]]
    for token in tokens:
        if token.value in {';', '|', '||', '&&', '\n'}:
            segments.append([])
        else:
            segments[-1].append(token)
    for segment in segments:
        args = []
        index = 0
        while index < len(segment):
            word = segment[index]
            if word.value in {'>', '>>', '>|', '<', '<>', '>&', '<&', '&>', '&>>'}:
                if index + 1 < len(segment):
                    paths.append((segment[index + 1], False))
                index += 2
                continue
            args.append(word)
            index += 1
        while args and (
            args[0].value == '!' or re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', args[0].value)
        ):
            args.pop(0)
        if not args:
            continue
        base = args.pop(0).value.rsplit('/', 1)[-1]
        if base in {'uv', 'npx', 'pnpm', 'npm', 'python', 'python3'}:
            if args and args[0].value in {'run', '-m'}:
                args.pop(0)
            if args:
                base = args.pop(0).value.rsplit('/', 1)[-1]
        if base in {'cargo', 'go', 'git', 'vitest'} and args and not args[0].value.startswith('-'):
            args.pop(0)
        if base in _DATA_COMMANDS:
            continue
        recursive = base in _TREE_COMMANDS
        search = base in _SEARCH
        path_options = _SEARCH_PATHS if search else _PATH_OPTIONS.get(base, set())
        data_options = _SEARCH_VALUES if search else _DATA_OPTIONS.get(base, set())
        tree_search = base in {'rg', 'ag'} or (search and any(
            re.fullmatch(r'-[a-zA-Z]*[rR][a-zA-Z]*', arg.value) or
            arg.value in {'--recursive', '--dereference-recursive', '--directories=recurse',
                          '-drecurse'}
            or (arg.value in {'-d', '--directories'} and position + 1 < len(args)
                and args[position + 1].value == 'recurse')
            for position, arg in enumerate(args)))
        positionals = []
        explicit_pattern = any(arg.value == '--files' for arg in args)
        uncertain = False
        index = 0
        options = True
        while index < len(args):
            word = args[index]
            value = word.value
            if options and value == '--':
                options = False
                index += 1
                continue
            flag = value.split('=', 1)[0]
            if options and value.startswith('-'):
                # Short options may be clustered, with the first value-taking
                # option owning the remaining bytes (`grep -ne.`, `sort -ofile`).
                attached = None
                if not value.startswith('--') and len(value) > 2:
                    value_flags = (path_options | data_options |
                                   (_CURL_DATA if base == 'curl' else set()))
                    for offset, letter in enumerate(value[1:], start=1):
                        candidate = '-' + letter
                        if candidate in value_flags:
                            flag = candidate
                            if offset + 1 < len(value):
                                attached = word.tail(offset + 1)
                            break
                if base == 'curl' and flag in _CURL_DATA:
                    if attached is not None:
                        data = attached
                    elif '=' in value:
                        data = word.tail(value.index('=') + 1)
                    else:
                        data = args[index + 1] if index + 1 < len(args) else None
                    if attached is None and '=' not in value:
                        index += 1
                    if data is not None and flag != '--data-raw':
                        file = _curl_file(data, flag)
                        if file is not None:
                            paths.append((file, False))
                elif flag in path_options:
                    if attached is not None:
                        paths.append((attached, False))
                    elif '=' in value:
                        paths.append((word.tail(value.index('=') + 1), False))
                    elif index + 1 < len(args):
                        paths.append((args[index + 1], False))
                        index += 1
                    if search and flag in {'-f', '--file'}:
                        explicit_pattern = True
                elif flag in data_options:
                    if search and flag in {'-e', '--regexp'}:
                        explicit_pattern = True
                    if attached is None and '=' not in value:
                        index += 1
                elif search and value.startswith(('-e', '-f')) and not value.startswith('--'):
                    explicit_pattern = True
                    if value.startswith('-f'):
                        paths.append((word.tail(2), False))
                elif (search and not re.fullmatch(r'-[nrilLRHIhsSuvFwocqaz]+', value)
                      and value not in {
                    '--files', '--hidden', '--no-ignore', '--no-ignore-vcs', '--follow',
                    '--recursive', '--dereference-recursive', '--line-number', '--count',
                    '--files-with-matches', '--files-without-match', '--fixed-strings',
                    '--ignore-case', '--smart-case', '--word-regexp', '--invert-match',
                    '--only-matching', '--no-heading', '--heading', '--null', '--null-data',
                }):
                    uncertain = True
                index += 1
                continue
            positionals.append(word)
            index += 1
        if search:
            positionals = positionals if explicit_pattern or uncertain else positionals[1:]
            if tree_search:
                roots.extend(positionals or [Word('.', '.', '.')])
                if uncertain:
                    roots.append(Word('.', '.', '.'))
        for word in positionals:
            if base == 'dd' and word.value.startswith(('if=', 'of=')):
                word = word.tail(3)
            if base in {'pytest', 'unittest', 'nose2', 'tox'} and '::' in word.value:
                end = word.value.index('::')
                word = word.prefix(end)
            paths.append((word, recursive))
    return paths, roots
