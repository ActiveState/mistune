import re
from .scanner import ScannerParser
from .util import (
    PUNCTUATION,
    LINK_LABEL,
    LINK_TITLE,
    LINK_BRACKET_HREF,
    PREVENT_BACKSLASH,
    ESCAPE_CHAR_RE,

    escape,
    escape_url,
    unikey,
)

HTML_TAGNAME = r'[A-Za-z][A-Za-z0-9-]*'
HTML_ATTRIBUTES = (
    r'(?:\s+[A-Za-z_:][A-Za-z0-9_.:-]*'
    r'(?:\s*=\s*(?:[^ "\'=<>`]+|\'[^\']*?\'|"[^\"]*?"))?)*'
)

LINK_LABEL_RE = re.compile(LINK_LABEL)
LINK_HREF_RE = re.compile(LINK_BRACKET_HREF)
LINK_TITLE_RE = re.compile(LINK_TITLE)
LINK_HREF_END_RE = re.compile(r'(?:\s+)|(?:' + PREVENT_BACKSLASH + '\))')

PAREN_START_RE = re.compile(r'\(\s*')
PAREN_END_RE = re.compile(r'\s*' + PREVENT_BACKSLASH + r'\)')


class InlineState:
    def __init__(self, block_state):
        self.tokens = []
        self.block_state = block_state
        self.in_link = False
        self.in_emphasis = False
        self.in_strong = False

    def copy(self):
        state = InlineState(self)
        state.in_link = self.in_link
        state.in_emphasis = self.in_emphasis
        state.in_strong = self.in_strong
        return state


class InlineParser:
    # PREVENT_BACKSLASH = r'(?<!\\)(?P<_slash>(?:\\\\)*)'

    AUTO_EMAIL = (
        r'''<[a-zA-Z0-9.!#$%&'*+\/=?^_`{|}~-]+@[a-zA-Z0-9]'''
        r'(?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?'
        r'(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*>'
    )

    # we only need to find the start pattern of an inline token
    SPECIFICATION = [
        # e.g. \`, \$
        ('escape', r'(?:\\[' + PUNCTUATION + '])+'),

        # `code, ```code
        ('codespan', r'`{1,}'),

        # *w, **w, _w, __w
        ('emphasis', r'\*{1,3}(?=[^\s*])|\b_{1,}(?=[^\s_])'),

        # [link], ![img]
        ('link', r'!?' + LINK_LABEL),

        # <https://example.com>. regex copied from commonmark.js
        ('auto_link', r'<[A-Za-z][A-Za-z0-9.+-]{1,31}:[^<>\x00-\x20]*>'),
        ('auto_email', AUTO_EMAIL),
    ]

    #: linebreak leaves two spaces at the end of line
    SOFT_LINEBREAK = r'(?:\\| {2,})\n(?!\s*$)'

    #: every new line becomes <br>
    HARD_LINEBREAK = r' *\n(?!\s*$)'

    INLINE_HTML = (
        r'(?<!\\)<' + HTML_TAGNAME + HTML_ATTRIBUTES + r'\s*/?>|'  # open tag
        r'(?<!\\)</' + HTML_TAGNAME + r'\s*>|'  # close tag
        r'(?<!\\)<!--(?!>|->)(?:(?!--)[\s\S])+?(?<!-)-->|'  # comment
        r'(?<!\\)<\?[\s\S]+?\?>|'
        r'(?<!\\)<![A-Z][\s\S]+?>|'  # doctype
        r'(?<!\\)<!\[CDATA[\s\S]+?\]\]>'  # cdata
    )

    def __init__(self, renderer, hard_wrap=False):
        self.renderer = renderer
        self.specification = list(self.SPECIFICATION)
        if hard_wrap:
            self.specification.append(('linebreak', self.HARD_LINEBREAK))
        else:
            self.specification.append(('linebreak', self.SOFT_LINEBREAK))

        self.__methods = {
            name: getattr(self, 'parse_' + name) for name, _ in self.specification
        }
        self._sc = None

    def _compile_sc(self):
        regex = '|'.join('(?P<%s>%s)' % pair for pair in self.specification)
        self._sc = re.compile(regex)

    def register_rule(self, name, pattern, method):
        self.specification.append((name, pattern))
        self.__methods[name] = lambda s, pos, state: method(self, s, pos, state)

    def parse_escape(self, m, state):
        text = m.group('escape')
        text = ESCAPE_CHAR_RE.sub(r'\1', text)
        state.tokens.append({
            'type': 'text',
            'raw': text,
        })
        return m.end()

    def parse_link(self, m, state):
        if state.in_link:
            # link can not be in link
            return

        pos = m.end()
        marker = m.group('link')

        if marker[0] == '!':
            token_type = 'image'
            text = marker[2:-1]
        else:
            token_type = 'link'
            text = marker[1:-1]

        if pos < len(m.string):
            c = m.string[pos]
            if c == '[':
                return self._parse_std_ref_link(m, token_type, text, state)
            elif c == '(':
                return self._parse_std_link(m, token_type, text, state)
            else:
                return self._parse_simple_ref_link(pos, token_type, text, state)
        return self._parse_simple_ref_link(pos, token_type, text, state)

    def _parse_simple_ref_link(pos, token_type, text, state):
        """A simple form of reference link::
        
            [an example]
            [an example]: https://example.com "optional title"
        """
        def_links = state.block_state.def_links

        key = unikey(text)
        if key not in def_links:
            return

        new_state = state.copy()
        new_state.in_link = True
        token = {
            'type': token_type,
            'children': self.render(text, new_state),
            'attrs': def_links[key],
        }
        state.tokens.append(token)
        return pos

    def _parse_std_ref_link(m, token_type, text, state):
        """Get link from references. The syntax looks like::

            [an example][id]

            [id]: https://example.com "optional title"
        """
        pos = m.end()
        m2 = LINK_LABEL_RE.match(m.string, pos)
        if m2:
            def_links = state.block_state.def_links
            key = m2.group(0)[1:-1]
            if not key:
                # [foo][]
                key = text

            key = unikey(text)
            if key not in def_links:
                return

            new_state = state.copy()
            new_state.in_link = True
            token = {
                'type': token_type,
                'children': self.render(text, new_state),
                'attrs': def_links[key],
            }
            state.tokens.append(token)
            return m2.end()
        # fallback to simple ref link
        return self._parse_simple_ref_link(pos, token_type, text, state)

    def _parse_std_link(m, token_type, text, state):
        """A standard link or image syntax::

            [text](/link "title")

            ![alt](/src "title")
        """
        pos = m.end()
        url, pos1 = self._parse_link_href(m.string, pos)

        marker = m.string[pos1 - 1]
        if marker == ')':
            # [text](<url>)
            # end without title
            new_state = state.copy()
            new_state.in_link = True
            url = ESCAPE_CHAR_RE.sub(r'\1', url)
            state.tokens.append({
                'type': token_type,
                'children': self.render(text, new_state),
                'attrs': {'url': escape_url(url)},
            })
            return pos1

        elif marker in ('"', '"'):
            m2 = LINK_TITLE_RE.match(m.string, pos1)
            if m2:
                m3 = PAREN_END_RE.match(m.string, m2.end())
                if m3:
                    new_state = state.copy()
                    new_state.in_link = True
                    title = m2.group(0)[1:-1]
                    title = ESCAPE_CHAR_RE.sub(r'\1', title)
                    url = ESCAPE_CHAR_RE.sub(r'\1', url)
                    state.tokens.append({
                        'type': token_type,
                        'children': self.render(text, new_state),
                        'attrs': {
                            'url': escape_url(url),
                            'title': title,
                        },
                    })
                    return m3.end()
        return self._parse_simple_ref_link(pos, token_type, text, state)

    def _parse_link_href(text, pos):
        m1 = PAREN_START_RE.match(text, pos)
        start_pos = m1.end()

        # </link-in-brackets>
        m2 = LINK_HREF_RE.match(text, start_pos)
        if m2:
            url = m2.group(0)[1:-1]
            m3 = LINK_HREF_END_RE.search(text, m2.end())
            return url, m3.end()

        # link not in brackets
        m3 = LINK_HREF_END_RE.search(text, start_pos)
        end_pos = m3.start()
        url = text[start_pos:end_pos]
        return url, m3.end()

    def parse_auto_link(self, m, state):
        return self._parse_auto_link(False, m, state)

    def parse_auto_email(self, m, state):
        return self._parse_auto_email(True, m, state)

    def _parse_auto_link(self, is_email, m, state):
        if state.in_link:
            return

        if is_email:
            text = m.group('auto_email')[1:-1]
            url = 'mailto:' + text
        else:
            text = m.group('auto_link')[1:-1]
            url = text

        children = self._call_render([{'type': 'text', 'raw': escape(text)}])
        state.tokens.append({
            'type': 'link',
            'children': children,
            'attrs': {'url': escape_url(url)},
        })
        return m.end()

    def parse_emphasis(self, m, state):
        pos = m.end()

        marker = m.group('emphasis')
        if len(marker) > 3:
            if state.in_emphasis or state.in_strong:
                return

            _slice = len(marker) - 3
            hole = marker[:_slice]
            marker = marker[_slice:]
        else:
            if len(marker) == 1 and state.in_emphasis:
                return
            elif len(marker) == 2 and state.in_strong:
                return
            hole = None

        pattern = re.compile(r'(.+)(?<=[^\s])' + re.escape(marker), re.S)
        m = pattern.match(m.string, pos)
        if m:
            if hole:
                state.tokens.append({'type': 'text', 'raw': hole})

            new_state = state.copy()
            text = m.group(1)
            if len(marker) == 1:
                new_state.in_emphasis = True
                children = self.render(text, new_state)
                state.tokens.append({'type': 'emphasis', 'children': children})
            elif len(marker) == 2:
                new_state.in_strong = True
                children = self.render(text, new_state)
                state.tokens.append({'type': 'strong', 'children': children})
            else:
                new_state.in_emphasis = True
                new_state.in_strong = True

                children = self._call_render([{
                    'type': 'strong',
                    'children': self.render(text, new_state)
                }])
                state.tokens.append({
                    'type': 'emphasis',
                    'children': children,
                })
            return m.end()

    def parse_codespan(self, m, state):
        marker = m.group('codespan')
        # require same marker with same length at end
        pattern = re.compile(r'(.+)(?<!`)' + marker + r'(?!`)', re.S)
        pos = m.end()

        m = pattern.match(m.string, pos)
        if m:
            code = m.group(1)
            state.tokens.append({'type': 'codespan', 'raw': code})
            return m.end()

    def parse_linebreak(self, m, state):
        state.tokens.append({'type': 'linebreak'})
        return m.end()

    def parse_inline_html(self, m, state):
        html = m.group(0)
        return 'inline_html', html

    def parse(self, s, pos, state):
        if not self._sc:
            self._compile_sc()

        while pos < len(s):
            m = self._sc.search(s, pos)
            if not m:
                break

            end_pos = m.start()
            if end_pos > pos:
                hole = s[pos:end_pos]
                state.tokens.append({'type': 'text', 'raw': hole})

            token_type = m.lastgroup
            func = self.__methods[token_type]
            new_pos = func(m, state)
            if not new_pos:
                pos = end_pos
                break
            pos = new_pos

        if pos < len(s):
            hole = s[pos:]
            state.tokens.append({'type': 'text', 'raw': hole})
        return state.tokens

    def render(self, s: str, state: InlineState):
        self.parse(s, 0, state)
        return self._call_render(state.tokens)

    def __call__(self, s, state):
        return self.render(s, InlineState(state))

    def _call_render(self, tokens):
        if self.renderer:
            return self.renderer.finalize(tokens)
        return list(data)
