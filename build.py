#!/usr/bin/env python3
"""Simple Jekyll builder for GitHub Pages preview."""

import os
import re
import sys
import yaml
import markdown
import shutil
import sass
from pathlib import Path

ROOT = Path(__file__).parent
PAGES_DIR = ROOT / "_pages"
INCLUDES_DIR = ROOT / "_includes"
LAYOUTS_DIR = ROOT / "_layouts"
DATA_DIR = ROOT / "_data"
SASS_DIR = ROOT / "_sass"
ASSETS_DIR = ROOT / "assets"
SITE_DIR = ROOT / "_site"

class JekyllContext:
    """Simulates Jekyll's variable context."""
    def __init__(self, page=None):
        self.vars = {"site": {}, "page": page or {}}

    def __getitem__(self, key):
        if key in self.vars:
            return self.vars[key]
        # Drill into nested: site.xxx.yyy
        parts = key.split(".")
        val = self.vars
        for p in parts:
            if isinstance(val, dict):
                val = val.get(p, None)
            else:
                return None
        return val

    def __setitem__(self, key, value):
        parts = key.split(".")
        d = self.vars
        for p in parts[:-1]:
            if p not in d or not isinstance(d[p], dict):
                d[p] = {}
            d = d[p]
        d[parts[-1]] = value

    def get(self, key, default=None):
        result = self[key]
        return result if result is not None else default


def read_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    # Handle YAML front matter (--- ... ---)
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
    if match:
        return yaml.safe_load(match.group(1)), content[match.end():]
    return yaml.safe_load(content), ""


def parse_front_matter(text):
    """Parse YAML front matter from text. Returns (front_matter_dict, body_text)."""
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
    if match:
        fm = yaml.safe_load(match.group(1)) or {}
        return fm, text[match.end():]
    return {}, text


def liquid_render(template, ctx, page_content=""):
    """Render a Liquid-style template with proper nesting support."""
    from urllib.parse import quote as url_quote

    # Preload all includes
    def get_include(name):
        inc_path = INCLUDES_DIR / name
        if inc_path.exists():
            return inc_path.read_text(encoding="utf-8")
        if ROOT / name:
            p = ROOT / name
            if p.exists():
                return p.read_text(encoding="utf-8")
        return ""

    # Resolve a value reference
    def resolve_value(expr, c):
        raw_expr = expr.strip()
        # split on | but not inside strings
        parts = re.split(r'\s*\|\s*', raw_expr)
        value_ref = parts[0].strip()
        filters = parts[1:]

        # Resolve the base value
        if (value_ref.startswith('"') and value_ref.endswith('"')) or \
           (value_ref.startswith("'") and value_ref.endswith("'")):
            result = value_ref[1:-1]
        elif value_ref.lower() in ("true", "false"):
            result = value_ref.lower() == "true"
        else:
            result = c[value_ref]

        for f in filters:
            result = _apply_filter(result, f.strip())
        return result

    def _apply_filter(value, filter_expr):
        if value is None:
            value = ""
        s = str(value)
        name = filter_expr.split(":")[0].split("(")[0].strip()
        rest = filter_expr[len(name):]

        if name == "markdownify":
            return markdown.markdown(s, extensions=['extra'])
        elif name == "strip_html":
            return re.sub(r'<[^>]+>', '', s)
        elif name == "strip_newlines":
            return s.replace('\n', ' ').replace('\r', '')
        elif name == "escape_once":
            return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        elif name == "default":
            m = re.search(r'default:\s*["\']?([^"\']*)["\']?', rest)
            d = m.group(1) if m else ""
            return s if s.strip() else d
        elif name == "prepend":
            m = re.search(r'prepend:\s*["\']?([^"\']*)["\']?', rest)
            p = m.group(1) if m else ""
            return p + s
        elif name == "append":
            m = re.search(r'append:\s*["\']?([^"\']*)["\']?', rest)
            a = m.group(1) if m else ""
            return s + a
        elif name == "replace":
            m = re.search(r'replace:\s*"([^"]*)"\s*,\s*"([^"]*)"', rest)
            if m:
                return s.replace(m.group(1), m.group(2))
            return s
        elif name == "url_encode":
            return url_quote(s)
        elif name == "capitalize":
            return s.capitalize()
        elif name == "downcase":
            return s.lower()
        elif name == "upcase":
            return s.upper()
        elif name == "size":
            return len(s)
        return s

    # Evaluate simple boolean conditions
    def eval_condition(cond_str, c):
        cond = cond_str.strip()
        # Handle "or"
        if " or " in cond:
            parts = cond.split(" or ")
            return any(eval_condition(p, c) for p in parts)
        # Handle "and"
        if " and " in cond:
            parts = cond.split(" and ")
            return all(eval_condition(p, c) for p in parts)
        # Truthy check
        val = c[cond]
        if isinstance(val, bool):
            return val
        if isinstance(val, (list, dict)):
            return len(val) > 0
        if isinstance(val, str):
            return len(val) > 0
        return val is not None

    # ---- Core: recursive block processing ----
    def process_blocks(text, c):
        """Process all Liquid tags recursively, handling proper nesting."""
        return _process_top_level(text, c, 0)

    def _process_top_level(text, c, start_pos):
        """Process text from start_pos, collecting output and returning output."""
        output = ""
        pos = start_pos
        while pos < len(text):
            # Find next tag (only {% tags, NOT {{ variables)
            tag_match = re.search(r'\{%-?\s*(\w+)', text[pos:])
            include_match = re.search(r'\{%-?\s*include\s+', text[pos:])

            # Find earliest match
            candidates = []
            if tag_match:
                candidates.append((tag_match.start() + pos, tag_match))
            if include_match:
                candidates.append((include_match.start() + pos, include_match))

            if not candidates:
                # No more tags, just output remaining and process {{ }}
                remaining = _process_variables(text[pos:], c)
                output += remaining
                pos = len(text)
                break

            # Get earliest match
            candidates.sort(key=lambda x: x[0])
            match_pos, match = candidates[0]

            # Output text before tag, with variable processing
            output += _process_variables(text[pos:match_pos], c)
            pos = match_pos

            # Check for include
            if re.match(r'\{%-?\s*include\s+([^\s%}]+)\s*-?%\}', text[pos:]):
                m = re.match(r'\{%-?\s*include\s+([^\s%}]+)\s*-?%\}', text[pos:])
                inc_name = m.group(1).strip()
                inc_content = get_include(inc_name)
                # Process includes recursively too
                inc_output = process_blocks(inc_content, c)
                output += inc_output
                pos += m.end()
                continue

            # Check for assign
            assign_m = re.match(r'\{%-?\s*assign\s+(\w+)\s*=\s*([^%}]+?)\s*-?%\}', text[pos:])
            if assign_m:
                var_name = assign_m.group(1).strip()
                var_value = assign_m.group(2).strip()
                c[var_name] = resolve_value(var_value, c)
                pos += assign_m.end()
                continue

            # Check for comment
            if re.match(r'\{%-?\s*comment\s*-?%\}', text[pos:]):
                end_comm = re.search(r'\{%-?\s*endcomment\s*-?%\}', text[pos:])
                if end_comm:
                    pos = pos + end_comm.end()
                else:
                    pos = len(text)
                continue

            # Check for if/elsif/else/endif
            if_m = re.match(r'\{%-?\s*if\s+([^%}]+?)\s*-?%\}', text[pos:])
            if if_m:
                condition = if_m.group(1).strip()
                pos += if_m.end()
                # Collect branches
                branch_output, pos = _process_if_block(text, pos, condition, c)
                output += branch_output
                continue

            # Check for for/endfor
            for_m = re.match(r'\{%-?\s*for\s+(\w+)\s+in\s+([^%}]+?)\s*-?%\}', text[pos:])
            if for_m:
                var_name = for_m.group(1).strip()
                array_src = for_m.group(2).strip()
                pos += for_m.end()
                loop_output, pos = _process_for_block(text, pos, var_name, array_src, c)
                output += loop_output
                continue

            # Unknown tag — skip it
            end_match = re.search(r'%\}', text[pos:])
            if end_match:
                pos += end_match.end()
            else:
                pos += 2  # safety

        return output

    def _process_if_block(text, pos, condition, c):
        """Process if/elsif/else/endif block, returns (output, new_pos)."""
        # Track nesting depths for all block types
        if_depth = 1
        for_depth = 0
        branches = [(condition, pos, None)]
        current_branch = 0

        while if_depth > 0 and pos < len(text):
            next_tag = re.search(r'\{%-?\s*(\w+)', text[pos:])
            if not next_tag:
                break
            tag_name = next_tag.group(1)
            tag_start = next_tag.start() + pos

            if tag_name in ('if', 'unless'):
                if_depth += 1
                pos = tag_start + next_tag.end()
            elif tag_name == 'for':
                for_depth += 1
                pos = tag_start + next_tag.end()
            elif tag_name == 'endfor':
                if for_depth > 0:
                    for_depth -= 1
                pos = tag_start + next_tag.end()
            elif tag_name == 'elsif' and if_depth == 1 and for_depth == 0:
                m = re.match(r'\{%-?\s*elsif\s+([^%}]+)\s*-?%\}', text[tag_start:])
                branches[current_branch] = (branches[current_branch][0],
                                            branches[current_branch][1], tag_start)
                pos = tag_start + (m.end() if m else len('{% elsif %}'))
                branches.append((m.group(1).strip() if m else '', pos, None))
                current_branch += 1
            elif tag_name == 'else' and if_depth == 1 and for_depth == 0:
                branches[current_branch] = (branches[current_branch][0],
                                            branches[current_branch][1], tag_start)
                m = re.match(r'\{%-?\s*else\s*-?%\}', text[tag_start:])
                pos = tag_start + (m.end() if m else len('{% else %}'))
                branches.append((None, pos, None))
                current_branch += 1
            elif tag_name == 'endif':
                if_depth -= 1
                if if_depth == 0:
                    branches[current_branch] = (branches[current_branch][0],
                                                branches[current_branch][1], tag_start)
                    m = re.match(r'\{%-?\s*endif\s*-?%\}', text[tag_start:])
                    pos = tag_start + (m.end() if m else len('{% endif %}'))
                    break
                else:
                    pos = tag_start + next_tag.end()
            else:
                pos = tag_start + next_tag.end()

        # Render matching branch
        for branch_cond, branch_start, branch_end in branches:
            if branch_end is None or branch_start >= branch_end:
                continue
            if branch_cond is None:
                return process_blocks(text[branch_start:branch_end], c), pos
            if eval_condition(branch_cond, c):
                return process_blocks(text[branch_start:branch_end], c), pos

        return "", pos

    def _process_for_block(text, pos, var_name, array_src, c):
        """Process for/endfor block, returns (output, new_pos)."""
        for_depth = 1
        if_depth = 0
        body_start = pos

        while for_depth > 0 and pos < len(text):
            next_tag = re.search(r'\{%-?\s*(\w+)', text[pos:])
            if not next_tag:
                break
            tag_name = next_tag.group(1)
            tag_start = next_tag.start() + pos

            if tag_name == 'for':
                for_depth += 1
                pos = tag_start + next_tag.end()
            elif tag_name == 'if' or tag_name == 'unless':
                if_depth += 1
                pos = tag_start + next_tag.end()
            elif tag_name == 'endif':
                if if_depth > 0:
                    if_depth -= 1
                pos = tag_start + next_tag.end()
            elif tag_name == 'endfor':
                for_depth -= 1
                if for_depth == 0:
                    body_end = tag_start
                    m = re.match(r'\{%-?\s*endfor\s*-?%\}', text[tag_start:])
                    pos = tag_start + (m.end() if m else len('{% endfor %}'))
                    break
                else:
                    pos = tag_start + next_tag.end()
            else:
                pos = tag_start + next_tag.end()
                pos += next_tag.end()

        # Get the array
        array = c[array_src]
        if not array or not isinstance(array, list):
            return "", pos

        # Expand body with forloop helper
        body_text = text[body_start:body_end]
        output = ""
        for i, item in enumerate(array):
            c[var_name] = item
            c["forloop"] = {
                "index": i + 1,
                "index0": i,
                "first": i == 0,
                "last": i == len(array) - 1,
                "length": len(array)
            }
            output += process_blocks(body_text, c)

        return output, pos

    def _process_variables(text, c):
        """Process {{ variable }} tags in plain text."""
        def replacer(m):
            expr = m.group(1).strip()
            try:
                result = resolve_value(expr, c)
            except Exception:
                return m.group(0)
            rv = "" if result is None else str(result)
            return rv
        return re.sub(r'\{\{-?\s*([^}]+?)\s*-?\}\}', replacer, text)

    # Do recursive processing
    return process_blocks(template, ctx)


def compile_scss():
    """Compile SCSS to CSS."""
    print("Compiling SCSS...")
    scss_file = ASSETS_DIR / "css" / "main.scss"
    out_file = SITE_DIR / "assets" / "css" / "main.css"

    if scss_file.exists():
        out_file.parent.mkdir(parents=True, exist_ok=True)
        # Strip Jekyll YAML front matter from scss files (can be empty ---\n---\n)
        scss_content = scss_file.read_text(encoding="utf-8")
        scss_content = re.sub(r'^---\s*\n(?:.*?\n)?---\s*\n', '', scss_content, count=1, flags=re.DOTALL)

        # Write temp file without front matter
        tmp_scss = ROOT / "_tmp_main.scss"
        tmp_scss.write_text(scss_content, encoding="utf-8")

        result = sass.compile(
            filename=str(tmp_scss),
            output_style="compressed",
            include_paths=[str(SASS_DIR)]
        )
        tmp_scss.unlink()  # Clean up temp
        out_file.write_text(result, encoding="utf-8")
        print(f"  Compiled: {out_file}")
    else:
        print("  No main.scss found")


def build_site():
    """Build the entire site."""
    print("=" * 50)
    print("Building site...")
    print("=" * 50)

    # 1. Load config
    print("\n[1/4] Loading config...")
    config = {}
    site_config = ROOT / "_config.yml"
    if site_config.exists():
        with open(site_config, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        print(f"  Loaded _config.yml: title='{config.get('title', 'N/A')}'")

    # 2. Load data files
    data = {}
    if DATA_DIR.exists():
        for data_file in DATA_DIR.glob("*.yml"):
            with open(data_file, "r", encoding="utf-8") as f:
                name = data_file.stem
                data[name] = yaml.safe_load(f) or {}
        for data_file in DATA_DIR.glob("*.yaml"):
            with open(data_file, "r", encoding="utf-8") as f:
                name = data_file.stem
                data[name] = yaml.safe_load(f) or {}
    print(f"  Loaded data: {list(data.keys())}")

    # 3. Get default layout template
    layout_file = LAYOUTS_DIR / "default.html"
    if not layout_file.exists():
        print("ERROR: default.html layout not found!")
        sys.exit(1)

    # 4. Clean and create _site
    if SITE_DIR.exists():
        try:
            shutil.rmtree(SITE_DIR)
        except:
            pass  # Skip if locked, overwrite below
    if not SITE_DIR.exists():
        SITE_DIR.mkdir()

    # 5. Copy assets
    print("\n[2/4] Copying assets...")
    if ASSETS_DIR.exists():
        target = SITE_DIR / "assets"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(ASSETS_DIR, target)
        print("  Copied assets/")

    # Copy images
    images_dir = ROOT / "images"
    if images_dir.exists():
        target = SITE_DIR / "images"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(images_dir, target)
        print("  Copied images/")

    # Copy other static files
    for item in ["favicon.ico", "favicon-32x32.png", "favicon-16x16.png",
                  "apple-touch-icon.png", "site.webmanifest", "browserconfig.xml"]:
        src = ROOT / item
        if src.exists():
            shutil.copy2(src, SITE_DIR / item)

    # 6. Compile SCSS
    print("\n[3/4] Compiling SCSS...")
    compile_scss()

    # Pre-process layouts: strip front matter and flatten layout chain
    def load_layout(name):
        layout_f = LAYOUTS_DIR / f"{name}.html"
        if not layout_f.exists():
            return None  # layout not found, skip
        content = layout_f.read_text(encoding="utf-8")
        fm, body = parse_front_matter(content)
        # If this layout extends another layout, load the parent and embed
        parent_name = fm.get("layout", None)
        if parent_name:
            parent = load_layout(parent_name)
            if parent:
                # Render parent with this layout's body as content
                return parent.replace("{{ content }}", body)
        return body

    # Load fully-resolved layout
    layout_content = load_layout("default")
    if not layout_content:
        # Fallback: load default.html directly, skipping any nested layout
        raw = layout_file.read_text(encoding="utf-8")
        _, layout_content = parse_front_matter(raw)
    print(f"  Layout resolved ({len(layout_content)} chars)")

    # 7. Build pages
    print("\n[4/4] Building pages...")

    # Setup context
    ctx = JekyllContext()
    ctx.vars["site"] = config
    ctx.vars["site"]["data"] = data
    ctx.vars["author"] = config.get("author", {})
    ctx.vars["domain"] = ""

    pages_built = 0
    if PAGES_DIR.exists():
        for page_file in sorted(PAGES_DIR.glob("*.md")):
            print(f"  Building: {page_file.name}")
            content = page_file.read_text(encoding="utf-8")

            # Parse front matter
            fm, body = parse_front_matter(content)

            # Set page context
            page_ctx = {**fm}
            page_url = fm.get("permalink", f"/{page_file.stem}/")
            page_ctx["url"] = page_url
            page_ctx["title"] = fm.get("title", page_file.stem.title())

            # We need request context with page
            new_ctx = JekyllContext()
            new_ctx.vars = {**ctx.vars}
            new_ctx.vars["page"] = page_ctx

            # Process Liquid in body BEFORE markdown conversion
            body_liquid_processed = liquid_render(body, new_ctx)
            html_body = markdown.markdown(body_liquid_processed, extensions=['extra', 'codehilite', 'toc'])
            new_ctx["content"] = html_body

            # Render through layout
            rendered = liquid_render(layout_content, new_ctx)

            # Write output
            out_path = SITE_DIR
            if page_url == "/":
                out_path = SITE_DIR / "index.html"
            elif page_url.startswith("/"):
                url_parts = page_url.strip("/").split("/")
                for part in url_parts[:-1]:
                    out_path = out_path / part
                out_path.mkdir(parents=True, exist_ok=True)
                out_path = out_path / (url_parts[-1] if url_parts[-1] else "index.html")
                if not out_path.suffix:
                    out_path = out_path / "index.html"
            else:
                out_path = SITE_DIR / page_url

            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(rendered, encoding="utf-8")
            pages_built += 1

            # Also create redirect pages if specified
            for redirect in fm.get("redirect_from", []):
                rpath = redirect.strip("/")
                if rpath:
                    redir_path = SITE_DIR / rpath
                    if not redir_path.suffix:
                        redir_path = redir_path / "index.html"
                    redir_path.parent.mkdir(parents=True, exist_ok=True)
                    redirect_html = f'<html><head><meta http-equiv="refresh" content="0; url={page_url}"></head><body></body></html>'
                    redir_path.write_text(redirect_html, encoding="utf-8")

            print(f"    -> {out_path.relative_to(SITE_DIR)}")

    # Copy 404 if exists
    notfound = ROOT / "404.html"
    if notfound.exists():
        shutil.copy2(notfound, SITE_DIR / "404.html")

    # Copy .nojekyll
    (SITE_DIR / ".nojekyll").write_text("")

    print(f"\nDone! Built {pages_built} pages.")
    print(f"Site ready at: {SITE_DIR}")
    return SITE_DIR


if __name__ == "__main__":
    build_site()
