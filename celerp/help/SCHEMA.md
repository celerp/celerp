# Help pages for Celerp modules

A module ships help by adding one key to its `PLUGIN_MANIFEST`:

```python
"help": {"dir": "my_module/help"},
```

The path is relative to the module folder and must stay inside it. Every `*.json` file in that
folder is one help page. Pages are read from every installed module, enabled or not, so a page
can explain how to turn its module on. They appear in `/help` and in the Celerp AI help tools
after the next module load.

Write pages in English. Name buttons, menus and fields by their translation catalog key, never by
their English text: the help viewer and Celerp AI show each label in the reader's language.

## Page

| Key | Type | Rule |
|---|---|---|
| `id` | string | `<module>.<page>`: the module name without `celerp-`, a dot, then lower case letters, digits and `-`. Core pages use `core.` |
| `title` | string | up to 120 characters |
| `summary` | string | one sentence |
| `keywords` | list of strings | words a reader might search for |
| `routes` | list of strings | the screens the page covers, as UI route patterns such as `/inventory/{entity_id}`; a list served per type names it (`/docs?type=invoice`), since a route covers only screens of its own `type` |
| `permission` | string or null | the permission key those screens need |
| `order` | whole number | position in the module's list |
| `steps` | list | 1 to 20 steps |
| `related` | list of strings | ids of related pages |
| `checked` | string, optional | the content hash of the screens the page covers when its steps were last read against them; written by `python -m celerp.help.checks stamp <page id>` |

No other keys are allowed, and a page file is at most 64 KB.

## Step

| Key | Type | Rule |
|---|---|---|
| `text` | string | up to 500 characters, with `{name}` placeholders for labels and no other braces |
| `labels` | object | maps every placeholder name in `text` to a catalog key, and nothing else |
| `image` | string or null | `img/<file>.webp` inside the help folder; required on every step of a first-party page |
| `capture` | object | how the screenshot is taken and how the step is checked |

`capture` has `route` (the screen to open), `selector` (the element the step is about and the
picture shows, its top 800 pixels when taller), `evidence` and `actions`, a list run in order before
the check. `evidence` is a non-empty list of what shows the step's state: each entry is a selector,
or `{name}` for the English text of one of the step's labels; a `{name}` inside a selector stands
for that text, so `.step-label--active:text-is('{map}')` asks for the active step to read the label. After the actions, every entry must
match a visible element lying wholly inside the pictured part of `selector`; a match elsewhere on
the page does not count.

```json
{"click": "<selector>"}
{"fill": "<selector>", "value": "<text>"}
{"upload": "<file input selector>", "value": "<text of a CSV file to choose>"}
{"press": "Escape"}
{"wait": "<selector>"}
```

## Example

```json
{
  "id": "inventory.add-item",
  "title": "Add an item",
  "summary": "Create a stock item.",
  "keywords": ["item", "stock", "product"],
  "routes": ["/inventory"],
  "permission": "view_inventory",
  "order": 1,
  "steps": [
    {
      "text": "Open {inv} from the sidebar.",
      "labels": {"inv": "nav.inventory"},
      "image": "img/add-item-1.webp",
      "capture": {"route": "/inventory", "selector": "main", "actions": [],
                  "evidence": ["main h1"]}
    }
  ],
  "related": []
}
```

## Checks

An invalid page in a third-party module is logged and skipped when modules load; the module's
other pages still load. First-party pages are held to the checks in `celerp/help/checks.py`
(schema, label keys, routes, permissions, images, screen coverage and the `checked` stamp) by
`tests/test_help_content.py`. Every signed-in screen of the app is covered by at least one page,
and every page covers at least one screen. A page's `checked` value is recomputed from the source
of the handlers serving the screens it covers (plus each function of the same module a handler
calls by name), so a change to one of those screens fails the check until the page's steps are
read against the screen again and `python -m celerp.help.checks stamp <page id>` writes the new
value. Each sidebar
and settings link a first-party module registers names its page: the nav entry's `help` (for
`href`) and `settings_help` (for `settings_href`) hold a page id the module ships, and that page's
`routes` must cover the link, so the screen's Help panel lists it. Screenshots are written only by
`tests/test_browser/test_help_capture.py` with `HELP_CAPTURE_WRITE=1`, which also checks each
step against the running app.
