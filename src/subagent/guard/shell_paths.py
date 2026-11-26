"""Shell path operands, retaining the distinction between quoting and expansion.

This is a metadata parser, never a shell evaluator. It only supplies credential
checks; the main classifier still decides whether the command may run.
"""

from __future__ import annotations

import codecs
import ctypes
import glob
import os
import re
import shlex
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
    # Bash's ANSI-C quoted strings are values, not shell variables. Decode them
    # before parsing so an escaped dot, tilde, or slash has the same protected
    # path meaning as its literal spelling.
    def decode_ansi(match: re.Match[str]) -> str:
        try:
            value = codecs.decode(match.group(1), "unicode_escape")
        except (UnicodeDecodeError, ValueError):
            # Malformed encodings remain conspicuous and will not match a path.
            value = match.group(1)
        return shlex.quote(value)

    command = re.sub(r"\$'((?:\\.|[^'\\])*)'", decode_ansi, command)
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
    'curl': {'-A', '--user-agent', '-u', '--user', '-U', '--proxy-user',
             '-X', '--request', '-e', '--referer', '-x', '--proxy', '-m', '--max-time',
             '-r', '--range', '-Y', '--speed-limit', '-y', '--speed-time',
             '--connect-timeout', '--retry', '--retry-delay', '--resolve', '--connect-to'},
}
_SEARCH = {'rg', 'ag', 'grep', 'egrep', 'fgrep'}
_DATA_COMMANDS = {'echo', 'printf', 'true', 'false', 'pwd', 'date'}
_TREE_COMMANDS = {'cp', 'mv', 'rm', 'rmdir', 'chmod', 'chown', 'shred', 'unlink', 'diff'}
_CURL_DATA = {'--data', '--data-binary', '--data-raw', '--data-urlencode', '-d',
              '--data-ascii', '--json', '--header', '-H',
              '--form', '-F', '--form-string', '-T', '--upload-file', '--variable'}
_CURL_SPACE = ' \t\r\n\v\f'
_CURL_SHORT_VALUES = {'-' + char for char in 'AbcCDeEHmopPQrTtUuwxyYzXKFd'}
_CURL_SWITCHES = {'--silent', '--show-error', '--fail', '--fail-with-body', '--insecure',
                  '--location', '--verbose', '--head', '--include', '--compressed',
                  '--http1.0', '--http1.1', '--http2', '--http3'}


class CurlFileSyntaxError(ValueError):
    """A file-bearing curl argument cannot safely be decoded."""


def _curl_form_files(word: Word, blanks: str = _CURL_SPACE) -> list[Word]:
    """Decode curl's multipart grammar after shell quoting, without opening files.

    Only @ attachments have comma lists. < fields and headers read one file;
    filename/type/encoder attributes are data. Inner quotes escape only quotes
    and backslashes. Retain shell expansion state for the decoded operands.
    """
    if '=' not in word.value:
        return []
    start = word.value.index('=') + 1
    attachment = word.value[start:start + 1] == '@'
    file_data = word.value[start:start + 1] in {'@', '<'}
    delimiters = ';,' if attachment else ';'
    pieces = re.findall('\x00.|[^\x00]', word.pattern, re.DOTALL)

    def field(index: int, quoted: bool = True) -> tuple[Word, int]:
        while index < len(word.value) and word.value[index] in blanks:
            index += 1
        decoded = []
        if quoted and word.value[index:index + 1] == '"':
            index += 1
            while index < len(word.value) and word.value[index] != '"':
                if (word.value[index] == '\\'
                        and word.value[index + 1:index + 2] in {'\\', '"'}):
                    index += 1
                decoded.append(index)
                index += 1
            if index == len(word.value):
                raise CurlFileSyntaxError('unclosed inner quote')
            index += 1
            while index < len(word.value) and word.value[index] not in delimiters:
                if word.value[index] not in _CURL_SPACE:
                    raise CurlFileSyntaxError('trailing text after inner quote')
                index += 1
        else:
            end = index
            while end < len(word.value) and word.value[end] not in delimiters:
                end += 1
            decoded = list(range(index, len(word.value[index:end].rstrip(blanks)) + index))
            index = end
        return Word(''.join(word.value[i] for i in decoded),
                    ''.join(pieces[i] for i in decoded), word.source), index

    files = []

    def add_file(file: Word, stdin_allowed: bool = True) -> None:
        if not file.value or len(files) >= 256:
            raise CurlFileSyntaxError('empty filename or too many form files')
        if file.value != '-' or not stdin_allowed:
            files.append(file)

    try:
        index = start + int(file_data)
        while True:
            value, index = field(index)
            if file_data:
                add_file(value)
            type_context = False
            while word.value[index:index + 1] == ';':
                index += 1
                while word.value[index:index + 1] and word.value[index] in blanks:
                    index += 1
                attribute = re.match(r'(filename|type|encoder|headers)=',
                                     word.value[index:], re.IGNORECASE)
                if attribute and attribute[1].lower() == 'type' and type_context:
                    attribute = None
                header_file = False
                if attribute:
                    index += len(attribute[0])
                    if attribute[1].lower() == 'type':
                        type_context = True
                        # Curl treats subsequent unknown attributes as raw MIME
                        # type continuations. Quotes do not hide their delimiters.
                        _, index = field(index, quoted=False)
                        continue
                    type_context = False
                    header_file = (attribute[1].lower() == 'headers'
                                   and word.value[index:index + 1] in {'@', '<'})
                    index += int(header_file)
                value, index = field(index, quoted=not type_context)
                if header_file:
                    add_file(value, stdin_allowed=False)  # headers=@- opens a file named '-'.
            if not attachment or word.value[index:index + 1] != ',':
                return files
            index += 1
    except CurlFileSyntaxError:
        # Literal data may use curl's permissive malformed-quote fallback, but
        # any possible file read must be decoded completely or hard-denied.
        if file_data or re.search(r';\s*headers=[@<]', word.value[start:], re.IGNORECASE):
            raise
        return []


def _curl_upload_files(word: Word, globoff: bool) -> list[Word]:
    """Curl expands upload brace lists and numeric/letter ranges even in quotes.

    Bound the Cartesian product at 256 names. Unrecognized/native glob syntax
    is hard-denied, rather than guessing which files curl might open.
    """
    if globoff or word.value in {'-', '.'}:
        return [] if word.value in {'-', '.'} else [word]
    names = [Word('', '', word.source)]
    pieces = re.findall('\x00.|[^\x00]', word.pattern, re.DOTALL)
    index = 0
    while index < len(word.value):
        char = word.value[index]
        if char == '\\' and word.value[index + 1:index + 2] in {'{', '}', '[', ']'}:
            choices = [Word(word.value[index + 1], quote_pattern(word.value[index + 1]),
                            word.source)]
            index += 2
        elif char == '{':
            if word.value[index + 1:index + 2] == '<':
                raise CurlFileSyntaxError('unsupported named upload glob')
            choices = []
            decoded = []
            index += 1
            while index < len(word.value):
                char = word.value[index]
                if char in '{[]':
                    raise CurlFileSyntaxError('unsupported nested upload glob')
                if char in ',}':
                    if not decoded or len(choices) >= 256:
                        raise CurlFileSyntaxError('empty or oversized upload brace list')
                    choices.append(Word(''.join(word.value[i] for i in decoded),
                                        ''.join(pieces[i] for i in decoded), word.source))
                    decoded = []
                    index += 1
                    if char == '}':
                        break
                    continue
                if char == '\\' and index + 1 < len(word.value):
                    index += 1  # Within a set curl unescapes any following character.
                decoded.append(index)
                index += 1
            else:
                raise CurlFileSyntaxError('unclosed upload brace list')
        elif char == '[':
            end = word.value.find(']', index + 1)
            match = re.fullmatch(r'(\d+)-(\d+)(?::(\d+))?|([a-zA-Z])-([a-zA-Z])(?::(\d+))?',
                                 word.value[index + 1:end]) if end >= 0 else None
            if not match:
                raise CurlFileSyntaxError('unsupported upload range')
            if any(len(part) > 20 for part in match.groups() if part):
                raise CurlFileSyntaxError('oversized upload range number')
            numeric = match[1] is not None
            low, high = ((int(match[1]), int(match[2])) if numeric else
                         (ord(match[4]), ord(match[5])))
            step = int((match[3] if numeric else match[6]) or '1')
            if not step or low > high or (high - low) // step + 1 > 256:
                raise CurlFileSyntaxError('invalid or oversized upload range')
            width = len(match[1]) if numeric and match[1].startswith('0') else 0
            values = (str(n).zfill(width) if numeric else chr(n)
                      for n in range(low, high + 1, step))
            choices = [Word(value, quote_pattern(value), word.source) for value in values]
            index = end + 1
        elif char in '}]':
            raise CurlFileSyntaxError('unmatched upload delimiter')
        else:
            end = index + 1
            while end < len(word.value) and word.value[end] not in '\\{}[]':
                end += 1
            choices = [word.tail(index).prefix(end - index)]
            index = end
        if len(names) * len(choices) > 256:
            raise CurlFileSyntaxError('too many upload filenames')
        names = [Word(name.value + part.value, name.pattern + part.pattern, word.source)
                 for name in names for part in choices]
    return names


def _curl_files(word: Word, flag: str) -> list[Word]:
    if flag in {'--data', '--data-binary', '--data-ascii', '--json', '-d',
                '--header', '-H'} and word.value.startswith('@'):
        return [] if word.value == '@-' else [word.tail(1)]
    if flag in {'--data-urlencode', '--variable'} and '@' in word.value:
        index = word.value.index('@')
        if '=' not in (word.value if flag == '--data-urlencode' else word.value[:index]):
            return [] if word.value[index + 1:] == '-' else [word.tail(index + 1)]
    if flag in {'--form', '-F'}:
        # Curl releases differ between ISBLANK and ISSPACE here. Check both
        # interpretations, so whitespace-named secrets are protected too.
        return list(dict.fromkeys(_curl_form_files(word) + _curl_form_files(word, ' \t')))
    return []


def _append_curl_uploads(paths: list[tuple[Word, bool]], uploads: list[Word],
                         globoff: bool, uncertain: bool) -> None:
    for upload in uploads:
        if uncertain and globoff:
            paths.append((upload, False))
        files = _curl_upload_files(upload, globoff and not uncertain)
        paths.extend((file, False) for file in files)
    uploads.clear()


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
        ls_recursive = base == 'ls' and any(
            arg.value == '--recursive' or re.fullmatch(r'-[a-zA-Z]*R[a-zA-Z]*', arg.value)
            for arg in args
        )
        recursive = base in _TREE_COMMANDS or ls_recursive
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
        curl_uploads: list[Word] = []
        curl_globoff = False
        curl_uncertain = False
        find_roots: list[Word] = []
        find_expression_started = False
        find_option_value = False
        index = 0
        options = True
        while index < len(args):
            word = args[index]
            value = word.value
            if base == 'find' and not find_expression_started:
                if find_option_value:
                    find_option_value = False
                elif value in {'-D', '-O'}:
                    find_option_value = True
                elif value in {'-H', '-L', '-P'} or re.fullmatch(r'-O[0-9]+', value):
                    pass
                elif value in {'!', '(', ')'} or value.startswith('-'):
                    find_expression_started = True
                else:
                    find_roots.append(word)
            if options and value == '--':
                options = False
                index += 1
                continue
            flag = value.split('=', 1)[0]
            curl_expand = base == 'curl' and flag.startswith('--expand-')
            if curl_expand:
                flag = '--' + flag[len('--expand-'):]
            if options and value.startswith('-'):
                # Short options may be clustered, with the first value-taking
                # option owning the remaining bytes (`grep -ne.`, `sort -ofile`).
                attached = None
                if not value.startswith('--') and len(value) > 2:
                    value_flags = (path_options | data_options |
                                   (_CURL_DATA | _CURL_SHORT_VALUES if base == 'curl' else set()))
                    for offset, letter in enumerate(value[1:], start=1):
                        candidate = '-' + letter
                        if base == 'curl' and candidate == '-:':
                            flag = '-:'
                            break  # Curl ignores the remaining letters after next.
                        if base == 'curl' and candidate == '-g':
                            curl_globoff = True
                        if candidate in value_flags:
                            flag = candidate
                            if offset + 1 < len(value):
                                attached = word.tail(offset + 1)
                            break
                if base == 'curl' and flag in {'-g', '--globoff', '--no-globoff', '--next', '-:'}:
                    if flag in {'--next', '-:'}:
                        _append_curl_uploads(paths, curl_uploads, curl_globoff, curl_uncertain)
                        curl_globoff = False
                        curl_uncertain = False
                    else:
                        curl_globoff = flag != '--no-globoff'
                elif base == 'curl' and flag in _CURL_DATA:
                    if attached is not None:
                        data = attached
                    elif '=' in value:
                        data = word.tail(value.index('=') + 1)
                    else:
                        data = args[index + 1] if index + 1 < len(args) else None
                    if attached is None and '=' not in value:
                        index += 1
                    if data is not None:
                        if (curl_expand and '{{' in data.value
                                and flag not in {'--form-string', '--data-raw'}):
                            raise CurlFileSyntaxError('unresolved curl variable expansion')
                        if flag in {'-T', '--upload-file'}:
                            curl_uploads.append(data)
                        else:
                            paths.extend((file, False) for file in _curl_files(data, flag))
                elif flag in path_options:
                    if curl_expand:
                        path_value = (attached.value if attached is not None else
                                      value.split('=', 1)[1] if '=' in value else
                                      args[index + 1].value if index + 1 < len(args) else '')
                        if '{{' in path_value:
                            raise CurlFileSyntaxError('unresolved curl path expansion')
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
                elif base == 'curl':
                    # An unknown option may own the next switch-looking word.
                    # It cannot establish that native upload globbing is off.
                    if (flag not in _CURL_SWITCHES
                            and not re.fullmatch(r'-[sSgfvkLIOiNJ#0aBqRGl?]+', value)):
                        curl_uncertain = True
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
        _append_curl_uploads(paths, curl_uploads, curl_globoff, curl_uncertain)
        if search:
            positionals = positionals if explicit_pattern or uncertain else positionals[1:]
            if tree_search:
                roots.extend(positionals or [Word('.', '.', '.')])
                if uncertain:
                    roots.append(Word('.', '.', '.'))
        if base == 'find':
            # `find` recursively visits its starting path even without an
            # explicit `-r` option. Every starting path before the expression
            # is a root; looking at only the first misses `find /tmp ~ ...`.
            roots.extend(find_roots or [Word('.', '.', '.')])
        for word in positionals:
            if base == 'dd' and word.value.startswith(('if=', 'of=')):
                word = word.tail(3)
            if base in {'pytest', 'unittest', 'nose2', 'tox'} and '::' in word.value:
                end = word.value.index('::')
                word = word.prefix(end)
            paths.append((word, recursive))
    return paths, roots
