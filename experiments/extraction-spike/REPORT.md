# Extraction spike A

Two ways of reading a docs.mistral.ai page were compared on 13 fixed pages:
the server-rendered HTML, and the React payload the browser fetches with the
`RSC: 1` header. Version 1 extracts the HTML. This report records what that
choice keeps, what it loses, and the evidence.

## Method

27 sequential GET requests with a `Docstral/0.1` User-Agent and a 0.5 s
delay: HTML and payload for each page, plus the MDX source of one page as
ground truth. The HTML route selects `main article.prose`; the payload route
converts the component tree and rejects any page with an unknown component.

## Results by page family

| Family                         | Pages                                           | HTML                                                                                 | Payload                                                              |
| ------------------------------ | ----------------------------------------------- | ------------------------------------------------------------------------------------ | -------------------------------------------------------------------- |
| Pages with tabbed code samples | chat-completion, agents-api, vision             | 6, 8, 4 code blocks: the active Python tab only                                      | 35, 57, 21 code blocks: every language and variant                   |
| Prose pages                    | model-lifecycle, admin-api overview, studio hub | complete, 3/3 tables                                                                 | identical                                                            |
| Data pages                     | models table, pricing                           | complete tables, 2/2 and 5/5                                                         | fails: table data lives in the site's JavaScript, not in the payload |
| Page with public MDX source    | install-setup                                   | 88.1% of source words, 4/8 exact code blocks, light/dark duplicates without language | 96.3%, 8/8 exact code blocks                                         |
| Generated API reference        | chat, beta/connectors                           | 0 operations recovered                                                               | 1 and 23 operations, 143 KB and 560 KB of Markdown                   |

The payload also failed on two pages whose data it does contain, because of
two components unknown to the converter. Two fetches straddled a site
redeploy; all 52 component imports on chat-completion were identical. The
site returns an ETag and answers `304` to `If-None-Match`.

## What version 1 keeps and loses

Kept, for every page in scope: the full prose, every table, section anchors,
and the default code sample of each example, which is Python, SDK V2,
synchronous, non-streaming.

Lost, by decreasing importance, about 7% of the information in scope:

1. Streaming and async code samples. The only loss that is not a translation
   of the Python sample: event format and asynchronous client differ.
2. TypeScript code samples. Same API call in another syntax.
3. cURL code samples. Raw HTTP form of the same call.
4. Closed FAQ answers, on a few pages such as platform-overview.
5. SDK V1 variants of the code samples.
6. Inactive install tabs: Windows and manual installation.
7. Diagrams, reduced to their alt text.

A question about a TypeScript or streaming sample receives the prose, the
citation of the page, and an explicit statement that the sample is not
indexed. No code is inferred from the Python sample. The evaluation dataset
labels such questions as out of coverage.

## Decision

Version 1 extracts rendered HTML with one extractor. Discovery starts from the
sitemap and follows in-scope internal links. Payload extraction stays in the
backlog until evaluation failures are attributable to hidden tabs or a demo
requires it. API reference pages are deferred; if admitted, they yield one
document per operation.
