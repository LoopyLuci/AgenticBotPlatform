"""The tree-sitter backend: exact symbols, ranges and references from the syntax tree, for every language the grammar
pack covers (Python, JavaScript, TypeScript/TSX, Rust, Go, Java, Kotlin, C#, Swift, Ruby, PHP).

It is optional: when `tree_sitter` or `tree_sitter_language_pack` cannot be imported (or a grammar will not load),
`available()` is false / a file is declined, and the Python `ast` parser and the regex extractors answer instead.

How a language is described: `_DECLS` maps a syntax-node type to the kind of symbol it declares; the name is the
node's `name` field (or the first identifier child for grammars without one). Containers (classes, impls, traits,
modules) give their members a parent, so qualified names read `Outer.inner`. Calls, plain names and attribute
accesses become references, each tied to the innermost symbol that contains it.
"""

from __future__ import annotations

import re
from typing import Optional

from bot.codeintel.parsers import (ImportRec, ParsedFile, Parser, RefRec, SymbolRec, _BUILTIN_NAMES, _NOT_CALLS,
                                   _short, MAX_DOC, MAX_SIGNATURE)

try:  # guarded: ABP runs without it
    from tree_sitter_language_pack import get_parser as _get_parser
except Exception:  # noqa: BLE001 - not installed, or a broken install: the other backends answer
    _get_parser = None

# suffix -> grammar name
SUFFIXES = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".rs": "rust", ".go": "go", ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".cs": "c_sharp",
    ".swift": "swift", ".rb": "ruby", ".php": "php",
}

_IDENTS = frozenset({"identifier", "type_identifier", "property_identifier", "field_identifier", "simple_identifier",
                     "shorthand_property_identifier", "namespace_identifier", "constant", "name", "package_identifier"})
_NAME_FALLBACK = ("identifier", "simple_identifier", "type_identifier", "name", "property_identifier", "constant")
_COMMENTS = frozenset({"comment", "line_comment", "block_comment", "multiline_comment", "doc_comment"})
_SKIP_SUBTREES = frozenset({"comment", "line_comment", "block_comment", "multiline_comment", "string", "string_literal",
                            "raw_string_literal", "interpreted_string_literal", "string_content", "template_string"})
_ATTR_FIELDS = ("attribute", "property", "field", "name", "member")

# node type -> symbol kind. "function" becomes "method" when the parent symbol is a class-like container.
_DECLS: dict[str, dict[str, str]] = {
    "python": {"function_definition": "function", "class_definition": "class"},
    "javascript": {"class_declaration": "class", "function_declaration": "function", "generator_function_declaration": "function",
                   "method_definition": "function"},
    "typescript": {"class_declaration": "class", "abstract_class_declaration": "class", "function_declaration": "function",
                   "generator_function_declaration": "function", "method_definition": "function",
                   "method_signature": "function", "abstract_method_signature": "function",
                   "interface_declaration": "interface", "type_alias_declaration": "type", "enum_declaration": "enum",
                   "internal_module": "namespace", "function_signature": "function"},
    "rust": {"function_item": "function", "function_signature_item": "function", "struct_item": "struct", "union_item": "struct",
             "enum_item": "enum", "trait_item": "trait", "impl_item": "impl", "mod_item": "module", "type_item": "type",
             "macro_definition": "macro"},
    "go": {"function_declaration": "function", "method_declaration": "function"},
    "java": {"class_declaration": "class", "interface_declaration": "interface", "enum_declaration": "enum",
             "record_declaration": "class", "annotation_type_declaration": "interface", "method_declaration": "function",
             "constructor_declaration": "function"},
    "kotlin": {"class_declaration": "class", "object_declaration": "class", "function_declaration": "function"},
    "c_sharp": {"class_declaration": "class", "interface_declaration": "interface", "struct_declaration": "struct",
                "enum_declaration": "enum", "record_declaration": "class", "namespace_declaration": "namespace",
                "method_declaration": "function", "constructor_declaration": "function"},
    "swift": {"class_declaration": "class", "protocol_declaration": "interface", "function_declaration": "function",
              "init_declaration": "function"},
    "ruby": {"class": "class", "module": "module", "method": "function", "singleton_method": "function"},
    "php": {"class_declaration": "class", "interface_declaration": "interface", "trait_declaration": "trait",
            "enum_declaration": "enum", "function_definition": "function", "method_declaration": "function"},
}
_DECLS["tsx"] = _DECLS["typescript"]
_CONTAINERS = frozenset({"class", "struct", "interface", "trait", "impl", "enum", "module", "namespace"})
_CALLS = {"call", "call_expression", "method_invocation", "invocation_expression", "function_call_expression",
          "member_call_expression", "scoped_call_expression", "macro_invocation"}
_IMPORTS = {"import_statement", "import_from_statement", "future_import_statement", "import_declaration", "use_declaration",
            "using_directive", "namespace_use_declaration", "import_header"}
_MEMBERISH = ("attribute", "member", "field_expression", "selector", "navigation", "scoped")
_QUOTED = re.compile(r"""['"]([^'"]+)['"]""")
_PATHISH = re.compile(r"(?:import|use|using|from)\s+(?:static\s+)?([\w.:\\/*{}, ]+?)\s*(?:;|$|as\b)", re.S)

_SPECIAL_TYPES = frozenset({"lexical_declaration", "variable_declaration", "type_spec", "expression_statement", "assignment",
                            "const_item", "static_item"})
_parsers: dict[str, object] = {}


def _parser(grammar: str):
    p = _parsers.get(grammar)
    if p is None and _get_parser is not None:
        try:
            p = _get_parser(grammar)
        except Exception:  # noqa: BLE001 - grammar missing / failed to load: decline the file
            p = False
        _parsers[grammar] = p
    return p or None


def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace") if node is not None and node.text is not None else ""


def _name_node(node, grammar: str):
    n = node.child_by_field_name("name")
    if n is not None:
        return n
    if node.type == "impl_item":
        return node.child_by_field_name("type")
    for c in node.children:
        if c.type in _NAME_FALLBACK:
            return c
    return None


def _callee_name(callee) -> str:
    """The final name of what is being called: `a.b.c()` -> c, `foo::bar()` -> bar."""
    node = callee
    for _ in range(8):
        if node is None:
            return ""
        if node.type in _IDENTS:
            return _text(node)
        nxt = None
        for f in _ATTR_FIELDS:
            nxt = node.child_by_field_name(f)
            if nxt is not None:
                break
        if nxt is None:
            named = [c for c in node.children if c.is_named]
            nxt = named[-1] if named else None
        node = nxt
    return ""


def _doc_before(node, source_lines: list[str]) -> str:
    """The comment immediately above a declaration (first meaningful line)."""
    prev = node.prev_sibling
    if prev is None or prev.type not in _COMMENTS or prev.end_point[0] < node.start_point[0] - 1:
        return ""
    for line in _text(prev).splitlines():
        line = line.strip().lstrip("/#*!<").strip().rstrip("*/").strip()
        if sum(c.isalpha() for c in line) >= 3 and not line.startswith(("-", "=")):
            return _short(line, MAX_DOC)
    return ""


def _py_doc(body) -> str:
    if body is None or not body.named_children:
        return ""
    first = body.named_children[0]
    if first.type == "expression_statement" and first.named_children:     # older grammars wrap it
        first = first.named_children[0]
    if first.type != "string":
        return ""
    content = next((c for c in first.children if c.type == "string_content"), None)
    raw = _text(content) if content is not None else _text(first).strip("rRbBuUfF").strip("\"'")
    for line in raw.splitlines():
        if line.strip():
            return _short(line.strip(), MAX_DOC)
    return ""


def _signature(node, source: bytes) -> str:
    body = node.child_by_field_name("body")
    if body is None:
        for c in node.children:
            if c.type in ("block", "statement_block", "declaration_list", "class_body", "function_body", "field_declaration_list",
                          "enum_body", "interface_body", "body_statement", "compound_statement"):
                body = c
                break
    end = body.start_byte if body is not None and body.start_byte > node.start_byte else node.end_byte
    text = source[node.start_byte:end].decode("utf-8", errors="replace")
    text = " ".join(text.split()).rstrip(":{ ").rstrip()
    if not text:
        text = _text(node).splitlines()[0] if _text(node) else ""
    if len(text) > MAX_SIGNATURE:
        text = text[: MAX_SIGNATURE - 1] + "…"
    return text


class TreeSitterParser(Parser):
    name = "tree-sitter"

    @staticmethod
    def available() -> bool:
        return _get_parser is not None

    def handles(self, suffix: str) -> bool:
        return _get_parser is not None and suffix in SUFFIXES

    def parse(self, suffix: str, source: str) -> Optional[ParsedFile]:
        grammar = SUFFIXES.get(suffix)
        parser = _parser(grammar) if grammar else None
        if parser is None:
            return None
        data = source.encode("utf-8", errors="replace")
        try:
            tree = parser.parse(data)
        except Exception:  # noqa: BLE001 - decline: another backend may still read it
            return None
        return _extract(tree.root_node, data, grammar)


def _extract(root, data: bytes, grammar: str) -> ParsedFile:
    out = ParsedFile(lang=grammar, parser="tree-sitter")
    decls = _DECLS.get(grammar, {})
    seen_refs: set[tuple[str, str, int]] = set()
    skip_ids: set[int] = set()      # callee identifiers already recorded as calls (consumed on first visit)
    lines = data.decode("utf-8", errors="replace").split("\n")
    skip_names = _BUILTIN_NAMES if grammar == "python" else _NOT_CALLS

    def add_ref(name: str, line: int, kind: str, sym: int) -> None:
        if len(name) < 2 or name in skip_names and kind != "attr":
            return
        key = (name, kind, sym)
        if key not in seen_refs:
            seen_refs.add(key)
            out.refs.append(RefRec(name, line, kind, sym))

    def add_symbol(kind: str, name: str, start_node, node, parent: int, signature: str, doc: str, qual: str = "",
                   decorators: str = "") -> int:
        if kind == "function" and parent >= 0 and out.symbols[parent].kind in _CONTAINERS:
            kind = "method"
        base = qual or (out.symbols[parent].qname if parent >= 0 else "")
        qname = f"{base}.{name}" if base else name
        out.symbols.append(SymbolRec(kind, name, qname, signature, doc, start_node.start_point[0] + 1,
                                     node.end_point[0] + 1, parent, decorators))
        return len(out.symbols) - 1

    # iterative depth-first walk; each frame is (node, enclosing symbol)
    stack = [(root, -1)]
    while stack:
        node, cur = stack.pop()
        t = node.type
        if t in _SKIP_SUBTREES:
            continue
        # ---- imports
        if t in _IMPORTS or (grammar == "ruby" and t == "call" and _text(node.child_by_field_name("method")) in ("require", "require_relative")):
            _record_import(node, grammar, out)
            continue
        # ---- declarations
        kind = decls.get(t)
        name_node = None
        if kind is not None:
            if grammar == "kotlin" and t == "class_declaration" and any(c.type == "interface" for c in node.children):
                kind = "interface"
            if grammar == "swift" and t == "class_declaration":
                kw = node.child_by_field_name("declaration_kind")
                kind = {"struct": "struct", "enum": "enum", "extension": "impl"}.get(_text(kw), "class")
            if grammar == "go" and t == "method_declaration":
                kind = "method"
            name_node = _name_node(node, grammar)
            if name_node is None and kind != "impl":
                kind = None
        if kind is not None and name_node is not None:
            name = _text(name_node)
            if grammar == "rust" and t == "impl_item":
                name = re.sub(r"<.*", "", name).strip() or name
            start_node = node
            decorators = ""
            doc = _py_doc(node.child_by_field_name("body")) if grammar == "python" else ""
            if grammar == "python" and node.parent is not None and node.parent.type == "decorated_definition":
                start_node = node.parent
                decorators = _short(", ".join(_text(c).strip() for c in node.parent.children if c.type == "decorator"), 160)
            elif node.parent is not None and node.parent.type in ("export_statement", "decorated_definition") and grammar != "python":
                start_node = node.parent
            if not doc:
                doc = _doc_before(start_node, lines)
            qual = ""
            if grammar == "go" and t == "method_declaration":
                recv = node.child_by_field_name("receiver")
                tid = re.findall(r"[A-Za-z_]\w*", _text(recv)) if recv is not None else []
                qual = tid[-1] if tid else ""
            sig = _signature(node, data)
            idx = add_symbol(kind, name, start_node, node, cur, sig, doc, qual, decorators)
            # the name node is a declaration, not a reference
            for child in reversed(node.named_children):
                if child.id != name_node.id:
                    stack.append((child, idx))
            continue
        # Go: type declarations, JS/TS arrow-function constants, uppercase constants
        special = _special_declaration(node, grammar, cur, out, add_symbol, lines, data) if t in _SPECIAL_TYPES else None
        if special is not None:
            for child, owner in reversed(special):
                stack.append((child, owner))
            continue
        # ---- references
        if t in _CALLS:
            callee = node.child_by_field_name("function") or node.child_by_field_name("method") or node.child_by_field_name("name")
            if callee is None and node.children:
                callee = node.children[0]
            if t == "method_invocation":
                callee = node.child_by_field_name("name")
            elif t == "macro_invocation":
                callee = node.child_by_field_name("macro")
            nm = _callee_name(callee)
            if nm == "require" and grammar in ("javascript", "typescript", "tsx"):
                q = _QUOTED.search(_text(node.child_by_field_name("arguments")))
                if q:
                    out.imports.append(ImportRec(q.group(1), "", 0, node.start_point[0] + 1))
            if nm:
                add_ref(nm, node.start_point[0] + 1, "call", cur)
                if callee is not None:
                    mark = callee
                    # the callee's final identifier was recorded as a call; do not record it again as a name
                    for _ in range(8):
                        nxt = next((mark.child_by_field_name(f) for f in _ATTR_FIELDS if mark.child_by_field_name(f) is not None), None)
                        if nxt is None:
                            break
                        mark = nxt
                    if mark.type in _IDENTS:
                        skip_ids.add(mark.id)
        elif t in _IDENTS and node.child_count == 0:
            if node.id in skip_ids:
                skip_ids.discard(node.id)
            elif not _is_declaring_position(node, grammar):
                parent = node.parent
                kind_ref = "name"
                if parent is not None and any(m in parent.type for m in _MEMBERISH):
                    for f in _ATTR_FIELDS:
                        fld = parent.child_by_field_name(f)
                        if fld is not None and fld.id == node.id:
                            kind_ref = "attr"
                            break
                add_ref(_text(node), node.start_point[0] + 1, kind_ref, cur)
            continue
        stack.extend((child, cur) for child in reversed(node.named_children))
    return out


def _is_declaring_position(node, grammar: str) -> bool:
    parent = node.parent
    if parent is None:
        return False
    pt = parent.type
    if pt in ("parameters", "typed_parameter", "default_parameter", "typed_default_parameter", "list_splat_pattern",
              "dictionary_splat_pattern", "formal_parameters", "required_parameter", "optional_parameter", "parameter",
              "lambda_parameters", "global_statement", "nonlocal_statement"):
        return True
    if pt in ("assignment", "augmented_assignment", "for_statement", "variable_declarator", "let_declaration"):
        for f in ("left", "name", "pattern"):
            fld = parent.child_by_field_name(f)
            if fld is not None and fld.id == node.id:
                return True
    if pt == "keyword_argument":
        fld = parent.child_by_field_name("name")
        return fld is not None and fld.id == node.id
    if pt in ("pair", "field_declaration", "enum_variant", "enumerator") and parent.child_by_field_name("name") is not None:
        return parent.child_by_field_name("name").id == node.id
    return False


def _special_declaration(node, grammar: str, cur: int, out: ParsedFile, add_symbol, lines, data):
    """Declarations that are not a plain `name` field: returns [(child, owner), ...] to keep walking, or None."""
    t = node.type
    if grammar in ("javascript", "typescript", "tsx") and t in ("lexical_declaration", "variable_declaration"):
        wrap = node.parent if node.parent is not None and node.parent.type == "export_statement" else node
        children = []
        for decl in node.named_children:
            if decl.type != "variable_declarator":
                continue
            nm = decl.child_by_field_name("name")
            val = decl.child_by_field_name("value")
            if nm is None or nm.type != "identifier":
                continue
            name = _text(nm)
            if val is not None and val.type in ("arrow_function", "function_expression", "function", "generator_function"):
                sig = " ".join(_text(node).splitlines()[0].split())
                idx = add_symbol("function", name, wrap, node, cur, _short(sig.rstrip("{ "), MAX_SIGNATURE), _doc_before(wrap, lines))
                children.append((val, idx))
            elif name.isupper() and len(name) > 1:
                add_symbol("variable", name, wrap, node, cur, _short(" ".join(_text(node).split()), MAX_SIGNATURE), _doc_before(wrap, lines))
                if val is not None:
                    children.append((val, cur))
            elif val is not None:
                children.append((val, cur))
        return children
    if grammar == "go" and t == "type_spec":
        nm = node.child_by_field_name("name")
        ty = node.child_by_field_name("type")
        if nm is None:
            return None
        kind = {"struct_type": "struct", "interface_type": "interface"}.get(ty.type if ty is not None else "", "type")
        owner_node = node.parent if node.parent is not None and node.parent.type == "type_declaration" and node.parent.named_child_count == 1 else node
        idx = add_symbol(kind, _text(nm), owner_node, node if owner_node is node else owner_node, cur,
                         _short(" ".join(_text(owner_node).split()[:6]), MAX_SIGNATURE), _doc_before(owner_node, lines))
        return [(ty, idx)] if ty is not None else []
    if grammar == "python" and (t == "expression_statement" or (t == "assignment" and node.parent is not None
                                                                   and node.parent.type in ("module", "block"))):
        asg = node
        if t == "expression_statement":
            asg = node.named_children[0] if node.named_children and node.named_children[0].type == "assignment" else None
        left = asg.child_by_field_name("left") if asg is not None else None
        parent_ok = cur < 0 or out.symbols[cur].kind == "class"
        if parent_ok and left is not None and left.type == "identifier" and _text(left).isupper() and len(_text(left)) > 1:
            add_symbol("variable", _text(left), node, node, cur, _short(_text(left), MAX_SIGNATURE), "")
            right = asg.child_by_field_name("right")
            return [(right, cur)] if right is not None else []
    if grammar == "rust" and t in ("const_item", "static_item"):
        nm = node.child_by_field_name("name")
        if nm is not None:
            add_symbol("variable", _text(nm), node, node, cur, _short(" ".join(_text(node).split()), MAX_SIGNATURE), _doc_before(node, lines))
            val = node.child_by_field_name("value")
            return [(val, cur)] if val is not None else []
    return None


def _record_import(node, grammar: str, out: ParsedFile) -> None:
    line = node.start_point[0] + 1
    t = node.type
    if grammar == "python":
        if t == "import_statement":
            for c in node.named_children:
                target = c.child_by_field_name("name") if c.type == "aliased_import" else c
                if target is not None and target.type == "dotted_name":
                    out.imports.append(ImportRec(_text(target), "", 0, line))
        elif t == "import_from_statement":
            mod = node.child_by_field_name("module_name")
            text = _text(mod)
            level = len(text) - len(text.lstrip("."))
            module = text.lstrip(".")
            found = False
            for c in node.named_children:
                if mod is not None and c.id == mod.id:
                    continue
                target = c.child_by_field_name("name") if c.type == "aliased_import" else c
                if target is not None and target.type in ("dotted_name", "identifier"):
                    out.imports.append(ImportRec(module, _text(target), level, line))
                    found = True
                elif c.type == "wildcard_import":
                    out.imports.append(ImportRec(module, "*", level, line))
                    found = True
            if not found and module:
                out.imports.append(ImportRec(module, "", level, line))
        return
    text = _text(node)
    m = _QUOTED.search(text)
    if grammar == "ruby":
        m = _QUOTED.search(text)
        if m:
            out.imports.append(ImportRec(m.group(1), "", 0, line))
        return
    if m and grammar in ("javascript", "typescript", "tsx", "go", "php", "swift") and t != "namespace_use_declaration":
        out.imports.append(ImportRec(m.group(1), "", 0, line))
        return
    for sub in text.splitlines() if grammar == "go" else [text]:
        m = _QUOTED.search(sub)
        if m:
            out.imports.append(ImportRec(m.group(1), "", 0, line))
    if grammar != "go":
        pm = _PATHISH.search(text.replace("\n", " "))
        if pm:
            out.imports.append(ImportRec(pm.group(1).strip(), "", 0, line))
