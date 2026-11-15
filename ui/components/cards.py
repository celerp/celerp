"""Shared record sections, using the document card's header and body styles."""
from fasthtml.common import Details, Div, H3, Span, Summary
from ui.components.icons import icon


def section_header(title, *, icon_name=None, extra=None, collapsible=False):
    tag = Summary if collapsible else Div
    return tag(
        Span(icon(icon_name), cls='section-icon') if icon_name else None,
        H3(title, cls='section-title'),
        extra,
        cls='section-header',
    )


def section_card(title, *children, icon_name=None, collapsible=False, header_extra=None,
                 body_cls=None, cls='', **attrs):
    tag = Details if collapsible else Div
    body = (Div(*children, cls=body_cls),) if body_cls else children
    return tag(section_header(title, icon_name=icon_name, extra=header_extra, collapsible=collapsible),
               *body, cls=f'doc-section {cls}'.strip(), **attrs)
