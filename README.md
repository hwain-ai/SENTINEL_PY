# SENTINEL_PY

Python 함수의 복잡도와 테스트 실행 범위로 CRAP을 계산하고, mutmut으로 테스트의 오류 탐지율을 측정합니다. `sentinel-tool/`의 어댑터가 통합 SENTINEL 요청을 받아 결과 JSON을 반환합니다.

## 검사 실행

[SENTINEL 설치 안내](https://github.com/hwain-ai/SENTINEL)를 따라 통합 명령을 준비한 뒤 검사할 프로젝트에서 실행합니다.

```sh
# Python 검사 도구와 프로젝트 설정 준비
sentinel setup --language python
# 기능 파일의 특정 함수를 지정한 테스트로 검사
sentinel check --file src/pricing.py --function calculate_discount --tests tests/test_pricing.py
# 프로젝트 설정의 기능 코드와 테스트 전체 검사
sentinel check --all
```

`--file`은 점수를 측정할 기능 파일, `--function`은 그 안의 함수 이름이며 괄호를 붙이지 않습니다. 함수를 생략하면 파일 전체를 측정합니다. `--tests`는 실행할 테스트 파일이고 여러 파일은 이 옵션을 반복합니다. 테스트를 생략하면 프로젝트에 설정된 테스트를 사용합니다. `--changed`는 Git 변경 파일 중 기능 코드만 선택하며, 테스트만 바뀌었으면 `noChanges`로 검사를 건너뜁니다. 이때는 기능 파일을 직접 지정해 재검사합니다.

기본 검사에는 자동 실행 시간 제한이 없습니다. 중단하려면 Ctrl+C를 누릅니다. 특정 파일·함수·테스트 검사 결과는 전체 인증으로 취급하지 않습니다. 점수, `inScope`, `pass`, `certified`와 오류 상태는 [결과 해석](https://github.com/hwain-ai/SENTINEL/blob/main/docs/results.md)을 참고합니다.

## 프로젝트 설정과 제한

모든 Python 파일을 기능 코드(`production`), 테스트(`testRoots`와 `testPatterns`), 제외 파일(`excluded`)로 구분해야 합니다. 분류되지 않은 파일은 `unclassifiedSource` 오류입니다. 제외 파일은 CRAP·변이 측정 대상에 포함하지 않습니다.

테스트에 외부 패키지가 필요하면 `sentinel setup --language python --python-requirements requirements/tests.txt`로 준비합니다. 검사기는 프로젝트의 `.sentinel-deps`에 설치한 의존성을 사본에 복사해 사용합니다. 이 폴더는 기능 코드·변이·coverage 측정에서 제외합니다.

CRAP 기본 상한은 8, mutation 최소 탐지율은 90%입니다. 함수와 중첩 함수는 각각 계산합니다. 실행 범위를 함수와 연결할 수 없으면 미측정으로 표시합니다. Mutation의 `killed`는 테스트의 기대값 검사 실패가 같은 결과로 재현된 변이만 셉니다. 실행 오류나 시간 초과는 탐지 성공으로 세지 않습니다.

Linux와 macOS의 x86_64·arm64를 지원하며 Windows는 WSL2에서 사용합니다. mutmut은 네이티브 Windows에서 실행하지 않습니다.

## 검사기 개발과 검증

`toolchain.lock.json`과 의존성 잠금이 사용할 도구를 정합니다. 실행기는 다운로드와 설치 파일의 지문을 확인하며, 기존 설치가 다르면 중단합니다. 고정 Python을 직접 실행하면 바이트코드 캐시가 설치 지문을 바꿀 수 있으므로 저장소 실행기를 사용합니다.

```sh
# 검사기 자체의 고정 Python·uv·의존성 준비
sentinel-tool/setup.sh
# 검사기 자체 시험 실행
./scripts/uv.sh run python -B -m unittest discover -s tests -v
```

문서와 개발 안내는 [문서 목록](docs/index.md)에 있습니다.

통합 실행기에 연결하는 어댑터 버전은 `0.1.3`이다. [sentinel-tool/version](sentinel-tool/version)과 설치한 실행기의 승인 목록을 함께 확인한다. 기존 설치의 갱신은 [통합 실행기 갱신 안내](https://github.com/hwain-ai/SENTINEL#승인된-도구-버전-갱신)를 따른다.
