# SENTINEL_PY

Python 프로젝트의 복잡도·테스트 실행 범위·변이 검사 결과를 함께 확인하는 언어별 검사기입니다. 변이 검사는 코드를 일부러 바꾼 뒤 테스트가 그 변경을 발견하는지 확인하는 방식입니다.

- 구현된 범위: 명령 실행, coverage·mutmut 도구 연결, 품질 판정과 실행 증거 기록.
- 현재 작업: 실제 공개 프로젝트의 검사 결과를 회수했고, 별도 사본에서 원본 mutmut의 직접 비교 실행을 진행 중입니다. 새 비교 결과는 아직 없습니다.
- 통합 연결: `sentinel-tool/` 폴더의 어댑터가 통합 SENTINEL의 도구 요청(표준입력 JSON)을 받아 이 검사기의 `check`를 실행하고 응답 JSON 하나만 표준출력에 씁니다. `sentinel setup --language python`이 `sentinel-tool/setup.sh`로 Python·uv·의존성을 준비한 뒤 이 어댑터를 묶음으로 설치합니다.
- 미완료 범위: 실제 도구 비교, 설치 플러그인의 호스트 검증. [최신 검증 기록](docs/sentinel-python-native-validation.md)을 기준으로 확인합니다.

## 역할

이 저장소는 Python 검사 도구를 실행하고 결과를 해석합니다. 컨테이너 생성·실행 제한·정리는 공통 SENTINEL이 맡습니다.

전체 검사는 소스 구조 확인, 테스트 실행 범위 수집, 점수 계산, 변이 검사 순서로 진행합니다. CRAP은 코드 복잡도와 테스트 실행 범위를 합친 점수이며, 반올림 없이 8 이하인지 판정합니다. 함수 안의 함수나 익명 함수도 각각 측정하고 바깥 함수와 중복 계산하지 않습니다.

프로젝트의 모든 Python 파일은 생산 코드(`production` 글롭), 테스트(`testRoots`+`testPatterns`), 또는 제외 파일(`excluded` 글롭, 예: `docs/**/*.py`) 중 하나로 분류돼야 하며, 분류되지 않은 파일이 있으면 `unclassifiedSource`로 중단합니다. 제외 파일은 분석·변이 대상이 아닙니다.

프로젝트 테스트가 외부 패키지를 쓰면 `./scripts/uv.sh deps <프로젝트>/.sentinel-deps <요구사항 파일>`로 검사기의 고정 Python에 맞는 wheel을 그 폴더에 설치합니다(통합 SENTINEL은 `setup --python-requirements`로 같은 일을 합니다). 검사 때 `.sentinel-deps`는 사본으로 복사되고 PYTHONPATH 마지막에 놓이며, 분석·변이·원본 보호·coverage 측정 대상에서는 빠집니다.

테스트 실행 범위는 고정된 coverage.py 7.16.0 보고서로 확인합니다. 형식·버전이 다르거나 검사 대상 코드를 제외한 보고서는 거부합니다. 함수별 실행 여부를 구분할 수 없으면 0%로 추측하지 않고 확인 불가로 남깁니다.

변이 검사는 다음 조건을 모두 만족해야 통과합니다.

- 검사할 변이가 하나 이상이고, 계획한 대상과 실제 결과가 정확히 일치합니다.
- 탐지된 변이의 비율이 최소 kill 비율 이상입니다. 기본값 100%에서는 모든 변이가 테스트의 기대값 검사 실패로 탐지되고 같은 실패를 재현할 수 있어야 합니다. 미탐지·시간 초과·실행 오류·도구 오류는 어느 비율에서도 탐지로 세지 않습니다.
- 승인받지 않은 검사 제외가 없습니다.

변경분만 검사하려면 `--changed-file 경로`(프로젝트 기준 상대 경로, 반복 가능)를 넘깁니다. 생산 코드에 해당하는 경로만 CRAP 측정과 변이 대상으로 남기고, 테스트는 전체를 그대로 실행합니다. 넘긴 경로 중 생산 코드가 하나도 없으면 판정할 대상이 없으므로 아무 검사도 돌리지 않고 통과(종료 0, `changedScope: empty`)로 응답하며 증거도 남기지 않습니다. 통합 SENTINEL의 `check --changed`가 git 변경분을 이 인자로 넘깁니다.

기준값은 `crap`, `mutation`, `check` 명령의 `--crap-max`(CRAP 상한, 기본 8)와 `--mutation-min`(최소 kill 비율 %, 기본 100)으로 넘깁니다. 정수 또는 소수점 두 자리까지의 문자열이며 정확한 분수로 비교합니다. 결과와 증거 파일의 crap·mutation 구성요소는 판정에 쓴 crapMax·mutationMin을 함께 기록합니다.

ItsDangerous 2.2.0의 실제 재검증에서 빌드와 기본 테스트 297개 두 번, 원본 보존과 컨테이너 정리를 확인했습니다. 이전 결과 저장 오류는 해소됐고 변이 567개의 상세 결과를 회수했습니다. 검사 결과는 품질 기준 미달인 종료 2/qualityFailed입니다. 결과 회수 완료를 프로젝트의 품질 통과로 해석하지 않습니다.

## 검증

고정된 Python 3.12.13·uv 0.12.9와 의존성이 이미 준비된 환경에서, 저장소 폴더 안에서 다음 명령을 실행합니다. 실행기는 도구 파일을 검증한 뒤 기존 설치만 사용하며 다운로드나 자동 설치를 하지 않습니다. 준비물이 없거나 잠금과 다르면 중단합니다.

```bash
# ./scripts/uv.sh run = 검증된 기존 환경에서 실행; python -B = 임시 바이트코드 파일 생성 금지
# -m unittest discover = 테스트 탐색·실행; -s tests = tests 폴더; -v = 개별 결과 표시
./scripts/uv.sh run python -B -m unittest discover -s tests -v
```

이 명령은 검사기 자체의 시험입니다. 공개 프로젝트 비교나 네트워크 차단 검증을 대신하지 않습니다.

## 설계 근거

원본 작업공간 설계 문서: [2026-08-native-quality-tools.md](https://github.com/hwain-ai/SENTINEL/blob/main/docs/design-docs/2026-08-native-quality-tools.md) (SENTINEL 저장소)
