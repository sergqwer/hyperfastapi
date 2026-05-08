# Test suite rules

Цей документ — обов'язкове читання перед написанням нового тесту або запуском суїти. Він фіксує конвенції, які гарантують, що тести залишаться стабільними коли ми перемкнемо їх з оригінального FastAPI на Rust-порт.

## Чому ця суїта існує

Проєкт `fastapi_rust` переписує FastAPI на Rust + PyO3 зі збереженням Python-сумісного API. Перед написанням Rust-коду нам потрібно зафіксувати "істину" про поведінку оригінального FastAPI у формі тестів, щоб:

1. **Знати, що ми відтворюємо** — кожен тест описує конкретний контракт публічного API.
2. **Виміряти, наскільки повільний baseline** — perf-тести в одному процесі показують GIL-стелю.
3. **Гарантувати поведінкову сумісність Rust-порту** — та сама суїта пройде проти Rust-порту незмінно.

## Як запускати

З кореня репозиторію:

| Команда | Що робить |
|---|---|
| `pytest tests/conformance` | Уся conformance-суїта (~30-40с з `-n auto`) |
| `pytest tests/conformance -n auto` | Те ж саме, паралельно по ядрах |
| `pytest tests/conformance -k <substring>` | Фільтр по імені тесту |
| `pytest tests/conformance -m type_fidelity` | Лише type-fidelity тести |
| `pytest tests/perf -m perf_single` | Лише single-process бенчмарки |
| `pytest tests/perf -m perf_multi` | Лише multi-process бенчмарки |
| `python -m perf.runner --scenario get_plain --mode both --duration 30` | Поза pytest, з консольним репортом |

Перший запуск встановлює залежності:

```powershell
pip install -e .\tests
```

## Як додавати conformance-тест

1. **Знайти аналог в upstream.** `reference/fastapi/tests/` — наша north-star. Якщо там вже є тест на цю фічу, копіюй стиль 1-в-1.
2. **App на рівні модуля.** Створюй `app = FastAPI()` нагорі файлу, не всередині функції. Це уникнення reuse-cost між тестами і відповідає upstream.
3. **`client = TestClient(app)` нижче за app.** Один TestClient на модуль.
4. **Назва тесту описова.** `test_query_required_str_missing_returns_422`, не `test_query_1` чи `test_basic`.
5. **Три рівні асертів — ОБОВ'ЯЗКОВО для кожного response-тесту:**
   - **Status code:** `assert response.status_code == 200, response.text`
   - **Body shape:** `assert response.json() == {...}` АБО `inline_snapshot`
   - **Type fidelity:** `assert response.headers["content-type"] == "application/json"` (для JSON-роутів)
6. **Validation error — повна структура.** Не зрізати кути:
   ```python
   assert response.json() == {
       "detail": [
           {
               "type": "missing",
               "loc": ["query", "p"],
               "msg": "Field required",
               "input": IsOneOf(None, {}),
           }
       ]
   }
   ```
7. **Параметризація через `@pytest.mark.parametrize`.** Не плодити майже однакові тести.

## Як додавати perf-сценарій

1. **Додати endpoint в `tests/perf/apps.py`** — у вже існуючий FastAPI-app, не створювати новий.
2. **Додати конфіг в `tests/perf/scenarios.py`** — `Scenario(name=..., method=..., path=..., body=..., headers=..., expected_status=200)`.
3. **Перевірити локально:**
   ```powershell
   python -m perf.runner --scenario <name> --mode single --duration 10
   ```
4. **Перевірити в pytest:**
   ```powershell
   pytest tests/perf -k <name>
   ```
5. **Threshold для регресій** — встановити мінімальний RPS у тесті (поки не надто агресивно: 200 RPS для single, 1000 RPS для multi). Це не absolute-perf-claim, це регресійний guard.

## Що тестувати ОБОВ'ЯЗКОВО для нового публічного API

- **Happy path** — successful 2xx з очікуваним body
- **Validation failure** — 4xx з повним правильним detail-форматом
- **Type fidelity** — content-type, кодування, тип content (bytes vs str)
- **OpenAPI representation** — snapshot test через `app.openapi()` або `/openapi.json`

## Що НЕ тестувати

- **Internal classes** з `_` префіксом — це private API FastAPI, контракт не гарантований.
- **Deprecated APIs** — лише deprecation warning (`pytest.warns(DeprecationWarning)`), не функціональність. Якщо upstream видалить — ми не повинні мати failing test.
- **Starlette/Pydantic internals** — це їхні власні проєкти; ми тестуємо лише public-API-точки FastAPI.

## Конвенції імпортів

```python
# YES:
from fastapi import FastAPI, Depends, Query
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse

# NO:
import fastapi
fastapi.FastAPI()  # імітує не-Pythonic style; реальні юзери так не роблять
```

## Заборонене

- **Mock-ові залежності в basic-тестах.** Тестуємо реальний flow. Використовуйте mock тільки для зовнішніх систем (сторонні HTTP API, бази даних).
- **Часткові асерти JSON.** `assert response.json()["foo"] == "bar"` без перевірки решти ключів — заборонено. Або повне порівняння (`== {...}`), або `inline_snapshot`.
- **Hardcoded абсолютні RPS-числа** як умова pass/fail у perf-тестах. Тільки relative thresholds (мінімум від абсолютного floor).
- **Залежність від таймінгу** в conformance-тестах. `time.sleep(0.5)` як affordance — заборонено. Використовуйте event-based синхронізацію (`asyncio.Event`, threading.Event).

## Інтерпретація результатів

Кожен прогін записує JSON у `tests/results/`:

- `tests/results/conformance/<timestamp>.json` — підсумок passed/failed по категоріях, список фейлів з тб
- `tests/results/perf/<timestamp>.json` — RPS і latency по кожному (scenario, mode) рядку

Для порівняння двох прогонів:

```powershell
python -m perf.compare tests\results\perf\baseline.json tests\results\perf\latest.json
```

Виведе markdown-таблицю з колонкою `ratio`. Ключове співвідношення для нашого проєкту:

> `rps(process_per_core) / rps(single_process)` — **GIL-efficiency factor**.
> Для FastAPI baseline ≈ N (кількість ядер). Для Rust-порту в одному процесі → 1.0 (вся ємність вже в одному процесі, додавання workers нічого не дає).

## Bombardier (load generator)

Perf-тести використовують [bombardier](https://github.com/codesenberg/bombardier) — Go-бінарник, не GIL-зв'язаний. На Windows немає стандартного пакету — варіанти встановлення:

1. **Recommended:** завантажити `bombardier-windows-amd64.exe` з [GitHub releases](https://github.com/codesenberg/bombardier/releases) у `tests/perf/bin/bombardier.exe` (gitignored).
2. **Через Go:** `go install github.com/codesenberg/bombardier@latest`
3. **Через Chocolatey:** `choco install bombardier`

Runner шукає бінарник у послідовності: `tests/perf/bin/bombardier.exe` → PATH → помилка з інструкцією.

## CI

GitHub Actions matrix (планується в Phase 5): Windows + Ubuntu × Python 3.11/3.12/3.13. Conformance — на кожен push, perf — окремий workflow раз на день / по тегу. Coverage threshold буде встановлений коли suite досягне 100% покриття публічних API.
