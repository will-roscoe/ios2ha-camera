"""A Sphinx directive that renders ``docs/home-assistant/entities.md``'s table
straight from the live entity registry.

``ios2ha_camera.entities.ENTITIES`` is the single declaration the supervisor
renders into MQTT discovery payloads and into the JSON API's
``GET /api/v1/objects``. The entities page asks to be generated from it rather
than typed out, precisely so a third, hand-kept copy can never drift from what
is actually published. This directive is that generation step: it imports the
registry at build time and turns it into one table per domain.

If the import fails, the table must not silently render empty -- that would
read as "the supervisor now publishes nothing", which is false; it would mean
this documentation build is broken. So a failure is logged through Sphinx's
own warning machinery instead of being swallowed: with the project's
``sphinx-build -W`` (every CI and local build here), a warning is fatal, and
the placeholder paragraph below never reaches a published page.
"""

from docutils import nodes
from docutils.parsers.rst import Directive
from sphinx.util.logging import getLogger

logger = getLogger(__name__)

# Declaration order in entities.py, and therefore publication order: cameras
# first, as it already documents ("the device's main feature ... before
# anything that could otherwise claim the device name").
_DOMAIN_TITLES = [
    ("camera", "Cameras"),
    ("number", "Numbers"),
    ("select", "Selects"),
    ("switch", "Switches"),
    ("button", "Buttons"),
    ("sensor", "Sensors"),
    ("binary_sensor", "Binary sensors"),
]

# One column spec per domain: (header, cell) pairs, where `cell` reads one
# value off an Entity and says whether it should render as inline code.
_COLUMNS = {
    "camera": [
        ("Entity", lambda e: (_entity_id(e), True)),
        ("Name", lambda e: (e.name, False)),
        ("Category", lambda e: (e.category, False)),
    ],
    "number": [
        ("Entity", lambda e: (_entity_id(e), True)),
        ("Name", lambda e: (e.name, False)),
        ("Range", lambda e: (f"{e.lo}–{e.hi}", False)),
        ("Step", lambda e: (e.step, False)),
        ("Unit", lambda e: (e.unit, False)),
        ("Category", lambda e: (e.category, False)),
    ],
    "select": [
        ("Entity", lambda e: (_entity_id(e), True)),
        ("Name", lambda e: (e.name, False)),
        ("Options", lambda e: (", ".join(e.options) if e.options else None, False)),
        ("Category", lambda e: (e.category, False)),
    ],
    "switch": [
        ("Entity", lambda e: (_entity_id(e), True)),
        ("Name", lambda e: (e.name, False)),
        ("Category", lambda e: (e.category, False)),
    ],
    "button": [
        ("Entity", lambda e: (_entity_id(e), True)),
        ("Name", lambda e: (e.name, False)),
        ("Category", lambda e: (e.category, False)),
    ],
    "sensor": [
        ("Entity", lambda e: (_entity_id(e), True)),
        ("Name", lambda e: (e.name, False)),
        ("Unit", lambda e: (e.unit, False)),
        ("Device class", lambda e: (e.device_class, False)),
        ("State class", lambda e: (e.state_class, False)),
        ("Precision", lambda e: (e.precision, False)),
        ("Category", lambda e: (e.category, False)),
    ],
    "binary_sensor": [
        ("Entity", lambda e: (_entity_id(e), True)),
        ("Name", lambda e: (e.name, False)),
        ("Device class", lambda e: (e.device_class, False)),
        ("Category", lambda e: (e.category, False)),
    ],
}


def _entity_id(e):
    return f"{e.domain}.ios2ha_camera_{e.object_id}"


def _cell(value, code):
    entry = nodes.entry()
    para = nodes.paragraph()
    if value is None:
        para += nodes.Text("—")
    elif code:
        para += nodes.literal(text=str(value))
    else:
        para += nodes.Text(str(value))
    entry += para
    return entry


def _table(domain, entities):
    columns = _COLUMNS[domain]
    table = nodes.table()
    tgroup = nodes.tgroup(cols=len(columns))
    table += tgroup
    for _ in columns:
        tgroup += nodes.colspec(colwidth=1)

    thead = nodes.thead()
    tgroup += thead
    header_row = nodes.row()
    for header, _getter in columns:
        header_row += _cell(header, code=False)
    thead += header_row

    tbody = nodes.tbody()
    tgroup += tbody
    for e in entities:
        row = nodes.row()
        for _header, getter in columns:
            value, code = getter(e)
            row += _cell(value, code)
        tbody += row

    return table


class EntitiesTable(Directive):
    """Usage: a bare ``entities-table`` directive with no arguments or content."""

    has_content = False
    required_arguments = 0
    optional_arguments = 0

    def run(self):
        try:
            from ios2ha_camera.entities import ENTITIES
        except Exception as exc:  # noqa: BLE001 - any import failure must surface
            logger.warning(
                "entities-table: could not import ios2ha_camera.entities (%s: %s); "
                "the generated entity table would otherwise render silently empty",
                type(exc).__name__, exc,
            )
            return [nodes.paragraph(text=f"Entity table unavailable: {exc}")]

        result = []
        for domain, title in _DOMAIN_TITLES:
            rows = [e for e in ENTITIES if e.domain == domain]
            if not rows:
                continue
            result.append(nodes.rubric(text=title))
            result.append(_table(domain, rows))
        return result


def setup(app):
    app.add_directive("entities-table", EntitiesTable)
    return {"parallel_read_safe": True, "parallel_write_safe": True}
