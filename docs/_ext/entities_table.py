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

# The same four columns for every domain. What an entity is for matters more
# than its metadata, so the description gets its own column and the rest is
# folded into one compact "Details" cell rather than a column per field, most of
# which would be empty for most domains.
_COLUMNS = [
    ("Entity", lambda e: (_entity_id(e), True)),
    ("Name", lambda e: (e.name if e.name is not None else "(device name)", False)),
    ("What it does", lambda e: (e.description or None, False)),
    ("Details", lambda e: (_details(e), False)),
]


def _number(v):
    """3600.0 reads as 3600, 0.01 stays 0.01."""
    return f"{v:g}" if isinstance(v, float) else str(v)


def _details(e):
    parts = []
    if e.lo is not None and e.hi is not None:
        parts.append(f"{_number(e.lo)}–{_number(e.hi)}, step {_number(e.step)}")
    if e.options:
        parts.append("options: " + ", ".join(e.options))
    if e.unit:
        parts.append(f"unit {e.unit}")
    if e.device_class:
        parts.append(f"class {e.device_class}")
    if e.category:
        parts.append(e.category)
    return "; ".join(parts) or None


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


def _table(entities):
    columns = _COLUMNS
    table = nodes.table()
    tgroup = nodes.tgroup(cols=len(columns))
    table += tgroup
    # Relative widths: the description is the column worth the room.
    for width in (3, 2, 6, 3):
        tgroup += nodes.colspec(colwidth=width)

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
            result.append(_table(rows))
        return result


def setup(app):
    app.add_directive("entities-table", EntitiesTable)
    return {"parallel_read_safe": True, "parallel_write_safe": True}
