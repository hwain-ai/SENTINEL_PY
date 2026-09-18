#!/usr/bin/env python3
"""Check documented paths and require related document edits for mapped changes.

This checks Git evidence of an edit, not whether the prose is semantically current.
The script uses only the Python standard library and can run in any Git repository.
"""
from __future__ import annotations

import argparse
import fnmatch
import html
import json
import os
from pathlib import Path
import posixpath
import re
import subprocess
import sys
from urllib.parse import quote, unquote, urlsplit


MANIFEST = "docs/manifest.json"
INDEX = "docs/index.md"


class LintError(Exception):
    pass


class Repository:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def git(self, *args: str, input_bytes=None, allow_failure=False):
        result = subprocess.run(
            ["git", "-c", "safe.directory=" + self.root.as_posix(), "-C", str(self.root), *args],
            input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode and not allow_failure:
            raise LintError(result.stderr.decode("utf-8", "replace").strip())
        return result

    def commit(self, ref: str) -> str:
        return self.git("rev-parse", "--verify", ref + "^{commit}").stdout.decode().strip()

    def tree(self, ref: str) -> str:
        return self.git("rev-parse", "--verify", ref + "^{tree}").stdout.decode().strip()

    def files(self, ref=None):
        if ref:
            output = self.git("ls-tree", "-r", "--name-only", "-z", ref).stdout
        else:
            output = self.git("ls-files", "--cached", "--others", "--exclude-standard", "-z").stdout
        paths = set(output.decode("utf-8").rstrip("\0").split("\0")) - {""}
        return paths if ref else {path for path in paths if (self.root / path).is_file()}

    def read(self, path: str, ref=None):
        if ref:
            result = self.git("show", ref + ":" + path, allow_failure=True)
            return result.stdout if result.returncode == 0 else None
        target = self.root / path
        return target.read_bytes() if target.is_file() else None

    def changes(self, base: str, head=None):
        args = ["diff", "--name-status", "-z", "--find-renames", base]
        if head:
            args.append(head)
        pieces = self.git(*args, "--").stdout.decode("utf-8").split("\0")
        paths = set()
        cursor = 0
        while cursor < len(pieces) and pieces[cursor]:
            status = pieces[cursor]
            cursor += 1
            count = 2 if status[0] in "RC" else 1
            paths.update(pieces[cursor:cursor + count])
            cursor += count
        if not head:
            paths.update(self.git("ls-files", "--others", "--exclude-standard", "-z").stdout.decode("utf-8").split("\0"))
        return paths - {""}


def valid_path(value, glob=False):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        return False
    if value.startswith("/") or any(part in ("", ".", "..") for part in value.split("/")):
        return False
    return not any(character in value for character in "\r\n\0") and (glob or not any(character in value for character in "*?[]"))


def matches(path: str, pattern: str) -> bool:
    """Match repository-relative glob segments; ** spans zero or more directories."""
    parts, patterns = path.split("/"), pattern.split("/")

    def visit(i, j):
        if j == len(patterns):
            return i == len(parts)
        if patterns[j] == "**":
            return visit(i, j + 1) or (i < len(parts) and visit(i + 1, j))
        return i < len(parts) and fnmatch.fnmatchcase(parts[i], patterns[j]) and visit(i + 1, j + 1)

    return visit(0, 0)


def load_manifest(repo: Repository, ref=None, optional=False):
    raw = repo.read(MANIFEST, ref)
    if raw is None and optional:
        return {"version": 1, "documents": [], "rules": [], "ignoreDocuments": []}
    if raw is None:
        raise LintError(MANIFEST + " is missing")
    try:
        manifest = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as error:
        raise LintError(MANIFEST + ": " + str(error)) from error
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise LintError(MANIFEST + ": version must be 1")
    if set(manifest) != {"version", "documents", "rules", "ignoreDocuments"}:
        raise LintError(MANIFEST + ": expected version, documents, rules, ignoreDocuments")
    if any(not isinstance(manifest[key], list) for key in ("documents", "rules", "ignoreDocuments")):
        raise LintError(MANIFEST + ": documents, rules and ignoreDocuments must be arrays")
    documents = set()
    for entry in manifest["documents"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "summary"}:
            raise LintError("Each document requires path and summary")
        path, summary = entry["path"], entry["summary"]
        if not valid_path(path) or not path.lower().endswith(".md") or path == INDEX:
            raise LintError("Invalid document path: " + repr(path))
        if path in documents:
            raise LintError("Duplicate document: " + path)
        if not isinstance(summary, str) or not summary.strip() or any(c in summary for c in "\n\r"):
            raise LintError("Document summary must be one nonempty line: " + path)
        documents.add(path)
    ids = set()
    for rule in manifest["rules"]:
        if not isinstance(rule, dict) or set(rule) != {"id", "sources", "documents"}:
            raise LintError("Each rule requires id, sources and documents")
        rule_id = rule["id"]
        if not isinstance(rule_id, str) or not rule_id.strip() or rule_id in ids:
            raise LintError("Invalid or duplicate rule id: " + repr(rule_id))
        ids.add(rule_id)
        for key in ("sources", "documents"):
            if not isinstance(rule[key], list) or not rule[key] or any(not valid_path(p, glob=key == "sources") for p in rule[key]):
                raise LintError(rule_id + ": " + key + " must contain relative paths/globs")
            if len(set(rule[key])) != len(rule[key]):
                raise LintError(rule_id + ": duplicate " + key)
        for path in rule["documents"]:
            if path not in documents:
                raise LintError(rule_id + ": unmapped document: " + path)
    if any(not valid_path(pattern, glob=True) for pattern in manifest["ignoreDocuments"]):
        raise LintError("ignoreDocuments must contain relative globs")
    return manifest


def render_index(manifest):
    lines = [
        "# 문서 안내", "",
        "각 문서의 내용과 함께 확인할 코드·설정 경로입니다.", "",
        "`docs/manifest.json`을 수정한 뒤 `python scripts/docs_lint.py --write-index`로 이 목록을 갱신합니다.",
        "코드 변경에 필요한 문서는 `python scripts/docs_lint.py --base HEAD`로 확인합니다.",
        "Python 명령은 환경에 맞게 Windows에서 `py -3`, Linux에서 `python3`로 바꿀 수 있습니다.",
        "검사는 관련 문서의 실제 변경 여부를 확인하며, 설명이 정확한지는 사람이 검토해야 합니다.", "",
        "| 문서 | 내용 | 관련 코드·설정 |", "| --- | --- | --- |",
    ]
    for entry in sorted(manifest["documents"], key=lambda item: item["path"]):
        path = entry["path"]
        sources = sorted({pattern for rule in manifest["rules"] if path in rule["documents"] for pattern in rule["sources"]})
        label = path.replace("|", "&#124;").replace("[", "&#91;").replace("]", "&#93;")
        link = quote(posixpath.relpath(path, "docs"), safe="/.-_")
        summary = entry["summary"].replace("|", "&#124;")
        related = ", ".join("`" + source.replace("|", "&#124;") + "`" for source in sources) or "직접 관리하는 안내 문서"
        lines.append("| [" + label + "](" + link + ") | " + summary + " | " + related + " |")
    return "\n".join(lines) + "\n"


def markdown_links(content):
    # Code examples may contain deliberately nonexistent sample paths.
    lines = []
    fence = None
    for line in content.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            lines.append("")
        else:
            lines.append("" if fence else line)
    text = re.sub(r"(`+).*?\1", "", "\n".join(lines))
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    destination = r"(<[^>\n]+>|(?:\\.|[^\s()\\]|\([^()\n]*\))+)"
    inline = re.compile(r"!?\[[^\]\n]*\]\(\s*" + destination + r"(?:\s+[\"'][^\n]*?[\"'])?\s*\)")
    reference = re.compile(r"^\s{0,3}\[[^\]\n]+\]:\s*" + destination, re.MULTILINE)
    for match in list(inline.finditer(text)) + list(reference.finditer(text)):
        yield match.group(1).strip("<>")


def normalized(raw):
    return raw.replace(b"\r\n", b"\n") if raw is not None else None


def document_content(raw):
    # Formatting-only whitespace edits are not evidence of a documentation update.
    return "".join(raw.decode("utf-8-sig").split()) if raw is not None else None


def check_structure(repo, manifest, ref=None):
    errors = []
    files = repo.files(ref)
    declared = {entry["path"] for entry in manifest["documents"]}
    ignored = manifest["ignoreDocuments"]
    expected = {path for path in files if path.lower().endswith(".md") and not any(matches(path, p) for p in ignored)}
    for path in sorted(expected - declared - {INDEX}):
        errors.append("Document is not indexed: " + path + " -> add a summary to " + MANIFEST)
    for path in sorted(declared - files):
        errors.append("Document does not exist: " + path)
    if normalized(repo.read(INDEX, ref)) != render_index(manifest).encode("utf-8"):
        errors.append(INDEX + " is stale -> run python scripts/docs_lint.py --write-index")
    for path in sorted((declared | {INDEX}) & files):
        try:
            content = repo.read(path, ref).decode("utf-8-sig")
        except UnicodeError:
            errors.append("Document is not UTF-8: " + path)
            continue
        for target in markdown_links(content):
            target = html.unescape(re.sub(r"\\([() ])", r"\1", target))
            try:
                parts = urlsplit(target)
            except ValueError:
                errors.append(path + ": invalid link " + target)
                continue
            if parts.scheme or parts.netloc or not parts.path:
                continue
            local = unquote(parts.path)
            resolved = posixpath.normpath(local.lstrip("/") if local.startswith("/") else posixpath.join(posixpath.dirname(path), local))
            if resolved == ".." or resolved.startswith("../"):
                errors.append(path + ": link leaves repository: " + target)
            elif resolved not in files and resolved != "." and not any(p.startswith(resolved + "/") for p in files):
                errors.append(path + ": broken local link: " + target)
    return errors


def check_freshness(repo, manifest, base, head=None):
    changed = repo.changes(base, head)
    previous = load_manifest(repo, base, optional=True)
    rules = {}
    for rule in previous["rules"] + manifest["rules"]:
        key = (rule["id"], tuple(rule["sources"]), tuple(rule["documents"]))
        rules[key] = rule
    errors = []
    for rule in rules.values():
        sources = sorted(path for path in changed if any(matches(path, pattern) for pattern in rule["sources"]))
        if not sources:
            continue
        updated = any(path != INDEX and document_content(repo.read(path, base)) != document_content(repo.read(path, head)) for path in rule["documents"])
        if not updated:
            errors.append("[" + rule["id"] + "] changed source: " + ", ".join(sources) + " -> update at least one related document: " + ", ".join(rule["documents"]))
    return errors


def check(repo, base=None, head=None):
    manifest = load_manifest(repo, head)
    errors = check_structure(repo, manifest, head)
    if base:
        errors.extend(check_freshness(repo, manifest, base, head))
    return errors


def new_branch_base(repo, head, remote):
    prefix = "refs/remotes/" + remote + "/" if remote else "refs/remotes/"
    refs = repo.git("for-each-ref", "--format=%(objectname)", prefix).stdout.decode().splitlines()
    if refs:
        result = repo.git("merge-base", head, *sorted(set(refs)), allow_failure=True)
        if result.returncode == 0:
            return result.stdout.decode().strip()
        if result.returncode != 1:
            raise LintError(result.stderr.decode("utf-8", "replace").strip())
    return repo.git("hash-object", "-w", "-t", "tree", "--stdin", input_bytes=b"").stdout.decode().strip()


def pre_push(repo, stream, remote):
    errors = []
    for line_number, line in enumerate(stream, 1):
        if not line.strip():
            continue
        values = line.split()
        if len(values) != 4:
            errors.append("pre-push input line " + str(line_number) + " must contain four fields")
            continue
        local_ref, local_sha, remote_ref, remote_sha = values
        if not all(re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha) for sha in (local_sha, remote_sha)):
            errors.append("pre-push input contains an invalid object id")
            continue
        if set(local_sha) == {"0"}:
            continue
        try:
            head = repo.commit(local_sha)
            base = new_branch_base(repo, head, remote) if set(remote_sha) == {"0"} else repo.commit(remote_sha)
            errors.extend(local_ref + " -> " + remote_ref + ": " + error for error in check(repo, base, head))
        except LintError as error:
            errors.append(local_ref + ": " + str(error))
    return errors


def ci_check(repo, environment):
    """Choose the comparison from the CI event, never from a dirty checkout."""
    event = environment.get("DOCS_EVENT", "")
    if event not in {"push", "pull_request", "workflow_dispatch"}:
        raise LintError("DOCS_EVENT must be push, pull_request or workflow_dispatch")
    deleted = environment.get("DOCS_DELETED", "")
    if deleted not in {"true", "false"}:
        raise LintError("DOCS_DELETED must be true or false")
    if deleted == "true" and event != "push":
        raise LintError("DOCS_DELETED=true is only valid for a push event")
    ref = environment.get("DOCS_REF", "")
    if not ref.startswith("refs/") or repo.git("check-ref-format", ref, allow_failure=True).returncode:
        raise LintError("DOCS_REF must be a valid full Git ref")
    default_branch = environment.get("DOCS_DEFAULT_BRANCH", "")
    if not default_branch or repo.git("check-ref-format", "refs/heads/" + default_branch, allow_failure=True).returncode:
        raise LintError("DOCS_DEFAULT_BRANCH must be a valid branch name")
    head_sha = environment.get("DOCS_HEAD", "")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head_sha):
        raise LintError("DOCS_HEAD must be a full commit object id")
    if deleted == "true":
        print("docs-lint: skipped deleted ref " + ref)
        return []
    if set(head_sha) == {"0"}:
        raise LintError("DOCS_HEAD cannot be zero for a non-deletion event")
    head = repo.commit(head_sha)
    if event == "workflow_dispatch":
        return check(repo, head=head)
    base_sha = environment.get("DOCS_BASE", "")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", base_sha):
        raise LintError("DOCS_BASE must be a full commit object id")
    if set(base_sha) == {"0"}:
        if event != "push":
            raise LintError("DOCS_BASE cannot be zero for a pull_request event")
        if not ref.startswith("refs/heads/"):
            raise LintError("A new push ref must be a branch to select the documentation baseline")
        if ref == "refs/heads/" + default_branch:
            base = repo.git("hash-object", "-w", "-t", "tree", "--stdin", input_bytes=b"").stdout.decode().strip()
            return check(repo, base, head)
        baseline_ref = "refs/remotes/origin/" + default_branch
        try:
            base = repo.commit(baseline_ref)
        except LintError as error:
            raise LintError("CI baseline " + baseline_ref + " is missing; fetch the default branch and its complete history before --ci") from error
    else:
        base = repo.commit(base_sha)
        if event == "push":
            return check(repo, base, head)
    # PRs and newly pushed feature branches contain changes since their common
    # ancestor. A target branch's later edits are not changes made by this branch.
    merged = repo.git("merge-base", base, head, allow_failure=True)
    if merged.returncode:
        raise LintError("CI baseline has no readable common ancestor with DOCS_HEAD; fetch complete history and use related branches (empty-tree fallback is disabled)")
    return check(repo, merged.stdout.decode().strip(), head)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Git repository root (default: current directory)")
    parser.add_argument("--check", action="store_true", help="Check inventory, index and local links")
    parser.add_argument("--write-index", action="store_true", help="Regenerate docs/index.md from docs/manifest.json")
    parser.add_argument("--base", help="Compare source/document changes with this Git commit or tree")
    parser.add_argument("--head", help="Inspect this committed tree instead of the working tree")
    parser.add_argument("--pre-push", action="store_true", help="Check the commit updates supplied by Git on stdin")
    parser.add_argument("--ci", action="store_true", help="Check submitted commits using DOCS_EVENT, DOCS_BASE, DOCS_HEAD, DOCS_REF, DOCS_DEFAULT_BRANCH and DOCS_DELETED")
    parser.add_argument("--remote", default="", help="Remote name supplied by the pre-push hook")
    args = parser.parse_args(argv)
    if args.ci and (args.check or args.base or args.head or args.write_index or args.pre_push or args.remote):
        parser.error("--ci cannot be combined with --check, --base, --head, --write-index, --pre-push or --remote")
    if args.head and not args.base:
        parser.error("--head requires --base")
    if args.pre_push and (args.base or args.head or args.write_index):
        parser.error("--pre-push cannot be combined with --base, --head or --write-index")
    if args.write_index and args.head:
        parser.error("--write-index cannot modify a committed --head")
    try:
        repo = Repository(args.root)
        if args.ci:
            errors = ci_check(repo, os.environ)
        elif args.pre_push:
            errors = pre_push(repo, sys.stdin, args.remote)
        else:
            head = repo.commit(args.head) if args.head else None
            base = repo.tree(args.base) if args.base else None
            if args.write_index:
                content = render_index(load_manifest(repo))
                target = repo.root / INDEX
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(".md.tmp")
                temporary.write_bytes(content.encode("utf-8"))
                os.replace(temporary, target)
            errors = check(repo, base, head) if args.check or args.base or not args.write_index else []
    except (LintError, OSError, UnicodeError) as error:
        errors = [str(error)]
    if errors:
        for error in errors:
            print("docs-lint: " + error, file=sys.stderr)
        return 1
    print("docs-lint: passed" + (" (index regenerated)" if args.write_index else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
