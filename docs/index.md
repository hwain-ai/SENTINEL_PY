# 문서 안내

각 문서의 내용과 함께 확인할 코드·설정 경로입니다.

`docs/manifest.json`을 수정한 뒤 `python scripts/docs_lint.py --write-index`로 이 목록을 갱신합니다.
코드 변경에 필요한 문서는 `python scripts/docs_lint.py --base HEAD`로 확인합니다.
Python 명령은 환경에 맞게 Windows에서 `py -3`, Linux에서 `python3`로 바꿀 수 있습니다.
검사는 관련 문서의 실제 변경 여부를 확인하며, 설명이 정확한지는 사람이 검토해야 합니다.

| 문서 | 내용 | 관련 코드·설정 |
| --- | --- | --- |
| [README.md](../README.md) | Python 검사기의 설치, 소스·테스트 설정, CRAP·mutation 실행과 결과 | `backend.lock.json`, `pyproject.toml`, `scripts/toolchain.py`, `sentinel-tool/**`, `src/**`, `toolchain.lock.json`, `uv.lock` |
| [docs/contributing.md](contributing.md) | 문서 색인·소스 연결표 관리, diff 검사와 push 훅 사용 | `.githooks/**`, `.github/workflows/**`, `docs/manifest.json`, `scripts/docs_lint.py`, `scripts/verify_repository.sh`, `tests/test_docs_lint.py` |
