"""Starting points for components that describe a service, not a document.

An MCP server card, an A2A agent card, OAuth resource metadata, a WebMCP
registration: each of these asserts that something exists and answers. Generating
one as a publishable file would be the tool inventing a capability, which is the
single thing it refuses to do — an agent reading an endpoint that does not answer
tries it, fails, and reports the client's site as broken.

So these are scaffolding for a developer and never artefacts. Every value that
would be a claim is an obvious placeholder, each carries a header saying it must
not be published, and `ComponentStatus.publishable` is false for all of them, so
no download route will serve one as final.

The transition out is the point. Once the operator declares the real endpoint in
onboarding and `verify_declared` confirms it answers, the component produces a
genuine artefact from the verified value and the template disappears. What is
here is what makes the work legible in the meantime, rather than a blank tab and
a specification link.
"""

from __future__ import annotations

import json

__all__ = ["PLACEHOLDER", "build_templates"]

# Deliberately shouty and deliberately not a plausible value. A template that
# looks finished is a template someone publishes.
PLACEHOLDER = "REPLACE_ME"

BANNER = (
    "This is a starting point, not a file to publish. Every REPLACE_ME below is a "
    "claim about a service that does not answer yet. Stand it up, enter its URL in "
    "onboarding, and the tool will generate the real file once it verifies."
)


def _json_template(body: dict) -> str:
    """JSON cannot carry a comment, so the warning goes in a field.

    Named `_comment` because that is the convention readers already expect, and
    placed first so it is the first thing seen in a diff or an editor.
    """
    return json.dumps({"_comment": BANNER, **body}, indent=2) + "\n"


def mcp_server_card(site_url: str, name: str = "") -> str:
    return _json_template(
        {
            "name": name or PLACEHOLDER,
            "description": PLACEHOLDER,
            "version": "0.1.0",
            "endpoint": f"https://mcp.{_host(site_url)}/{PLACEHOLDER}",
            "transport": "streamable-http",
            "capabilities": {"tools": True, "resources": True, "prompts": False},
            "authentication": {
                "type": "oauth2",
                "resourceMetadata": f"{site_url}/.well-known/oauth-protected-resource.json",
            },
        }
    )


def a2a_agent_card(site_url: str, name: str = "") -> str:
    return _json_template(
        {
            "name": name or PLACEHOLDER,
            "description": PLACEHOLDER,
            "url": f"{site_url}/{PLACEHOLDER}",
            "version": "0.1.0",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "skills": [
                {
                    "id": PLACEHOLDER,
                    "name": PLACEHOLDER,
                    "description": PLACEHOLDER,
                    "tags": [PLACEHOLDER],
                }
            ],
        }
    )


def oauth_protected_resource(site_url: str, name: str = "") -> str:
    return _json_template(
        {
            "resource": site_url,
            "authorization_servers": [f"https://auth.{_host(site_url)}"],
            "bearer_methods_supported": ["header"],
            "scopes_supported": [PLACEHOLDER],
            "resource_documentation": f"{site_url}/docs/{PLACEHOLDER}",
        }
    )


def skills_json(site_url: str, name: str = "") -> str:
    return _json_template(
        {
            "skills": [
                {
                    "name": PLACEHOLDER,
                    "description": PLACEHOLDER,
                    "endpoint": f"{site_url}/api/{PLACEHOLDER}",
                    "parameters": {},
                }
            ]
        }
    )


def api_catalog(site_url: str, name: str = "") -> str:
    return _json_template(
        {
            "linkset": [
                {
                    "anchor": site_url,
                    "service-desc": [
                        {
                            "href": f"{site_url}/{PLACEHOLDER}.json",
                            "type": "application/vnd.oai.openapi+json",
                        }
                    ],
                }
            ]
        }
    )


def webmcp_snippet(site_url: str, name: str = "") -> str:
    """A front-end change rather than a file, so it is emitted as code.

    Kept minimal on purpose. The value is in showing where the registration goes
    and what shape a tool takes; a fuller example would be guessing at the site's
    own functions and would need rewriting rather than filling in.
    """
    return (
        "<!-- " + BANNER + " -->\n"
        "<script>\n"
        "  // Register the page's own capabilities with an agent driving the browser.\n"
        "  if (navigator.modelContext) {\n"
        "    navigator.modelContext.registerTool({\n"
        f'      name: "{PLACEHOLDER}",\n'
        f'      description: "{PLACEHOLDER}",\n'
        "      inputSchema: { type: 'object', properties: {} },\n"
        "      async execute(input) {\n"
        "        // Call the same function the page's own UI calls.\n"
        f"        throw new Error('{PLACEHOLDER}: not implemented');\n"
        "      },\n"
        "    });\n"
        "  }\n"
        "</script>\n"
    )


def link_header_snippet(site_url: str, name: str = "", platform: str = "") -> str:
    """Server config for the Link header, in the syntax the host actually uses.

    The last component that handed a developer a paragraph. Two platforms cannot
    do it at all, and saying so is more useful than a snippet they would spend an
    afternoon failing to apply.
    """
    host = _host(site_url)
    if platform in ("shopify", "wix", "squarespace"):
        return f"""# {platform.title()} does not allow custom response headers on the
# primary domain. This component is not achievable there without putting a
# reverse proxy in front of the site, which is a larger decision than this file.
"""

    return f"""# Not a file to publish as-is: apply whichever block matches your host.

# Cloudflare Pages / Netlify -- _headers at the site root
/*
  Link: </llms.txt>; rel="describedby"; type="text/markdown"
  Link: </sitemap.xml>; rel="sitemap"

# nginx
#   add_header Link '</llms.txt>; rel="describedby"; type="text/markdown"' always;

# Apache
#   Header add Link "</llms.txt>; rel=\\"describedby\\"; type=\\"text/markdown\\""

# Confirm with: curl -sI https://{host}/ | grep -i '^link:'
"""


def markdown_negotiation_snippet(site_url: str, name: str = "", platform: str = "") -> str:
    """Return markdown when the request asks for it.

    The one component with measured benefit rather than assumed: roughly a
    five-fold drop in payload for agents already fetching the site. There is no
    portable implementation, so this shows the shape in both places it can live.
    """
    host = _host(site_url)
    return f"""# Not a file to publish: this is where the behaviour goes, not a document.

# Cloudflare -- a Transform Rule on the response, when
#   http.request.headers['accept'][0] contains 'text/markdown'

# Or in the application, wherever the response is built:
#
#   accept = request.headers.get('accept', '')
#   if 'text/markdown' in accept:
#       return Response(render_markdown(page), media_type='text/markdown')

# It counts as negotiated only when BOTH the content type and the payload
# change. Many sites return the same HTML under a markdown header, which is
# worse than not offering it -- the agent pays for the request and gains nothing.

# Confirm with:
#   curl -s https://{host}/ | wc -c
#   curl -s -H 'Accept: text/markdown' https://{host}/ | wc -c
"""


BUILDERS = {
    "link-header": link_header_snippet,
    "markdown-negotiation": markdown_negotiation_snippet,
    "mcp-card": mcp_server_card,
    "a2a-card": a2a_agent_card,
    "oauth-resource": oauth_protected_resource,
    "skills": skills_json,
    "api-catalog": api_catalog,
    "webmcp": webmcp_snippet,
}


def _host(site_url: str) -> str:
    return site_url.split("//")[-1].strip("/").removeprefix("www.")


def build_templates(site_url: str, site_name: str = "", platform: str = "") -> dict[str, str]:
    """Every template, keyed by component. Cheap enough to build unconditionally."""
    built: dict[str, str] = {}
    for key, builder in BUILDERS.items():
        try:
            built[key] = builder(site_url, site_name, platform)
        except TypeError:
            # The service templates take no platform; only the two server-config
            # ones vary by host.
            built[key] = builder(site_url, site_name)
    return built
