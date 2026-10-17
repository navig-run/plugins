# Third-party notices

navig-bookmarks contains code adapted from the following project.

## linkding

- Source: https://github.com/sissbruecker/linkding
- License: MIT
- Adapted (rewritten, not copied verbatim):
  - `bookmarks/services/parser.py` (Netscape bookmark HTML parser) -> `navig_bookmarks/netscape.py`;
    extended here with an `<H3>` folder stack so folders survive import.
  - `bookmarks/services/exporter.py` (Netscape bookmark HTML exporter) -> `navig_bookmarks/netscape.py`;
    extended here to write nested `<H3>` folders.
  - `bookmarks/utils.py` `normalize_url` -> `navig_bookmarks/urls.py`; extended with tracking
    parameter removal and default-port stripping.
  - `bookmarks/services/auto_tagging.py` (domain/path rule tagging) -> `navig_bookmarks/rules.py`;
    the domain match here requires a label boundary (`example.com` does not match
    `notexample.com`).
  - `bookmarks/services/website_loader.py` (head-only fetching) -> `navig_bookmarks/checker.py`;
    the SSRF guard is navig-sdk's (`navig_sdk.ssrf`), re-checked on every redirect hop.

```
The MIT License (MIT)

Copyright (c) 2019 Sascha Ißbrücker

Permission is hereby granted, free of charge, to any person obtaining a copy of this software
and associated documentation files (the "Software"), to deal in the Software without
restriction, including without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```
