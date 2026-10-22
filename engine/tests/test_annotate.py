import pytest

from codesync.annotate import (
    AnnotationCache,
    AnnotationOptions,
    AnnotationStatus,
    cache_key,
    plan_annotations,
    run_annotations,
)
from codesync.index import build_index
from codesync.llm import ProviderError
from codesync.resolve import Resolver
from codesync.variables import VariableKind
from codesync.varscan import build_variable_table

OK, MISSING = AnnotationStatus.OK, AnnotationStatus.MISSING
FAILED, SKIPPED = AnnotationStatus.FAILED, AnnotationStatus.SKIPPED


@pytest.fixture
def shop(shop_project):
    index = build_index(shop_project)
    resolver = Resolver(index)
    return resolver, build_variable_table(index, resolver)


def plan_for(shop, **options):
    resolver, table = shop
    return plan_annotations(resolver, table, AnnotationOptions(**options))


def test_every_variable_is_either_planned_static_or_skipped(shop):
    _, table = shop

    plan = plan_for(shop)

    assert plan.described + len(plan.static) + len(plan.skipped) == len(table)
    assert len(plan.static) == 4


def test_receivers_are_described_without_the_model(shop):
    plan = plan_for(shop)

    by_name = {item.variable: item for item in plan.static}
    method = by_name["shop.cart.Cart.add.self"]
    builder = by_name["shop.cart.Cart.build.cls"]

    assert (method.status, method.model, method.role) == (OK, "static", "receiver")
    assert method.type == "shop.cart.Cart"
    assert builder.purpose == "The class the method is called on."


def test_requests_respect_the_variable_limit(shop):
    plan = plan_for(shop, max_variables=6)

    assert len(plan.requests) > 1
    assert all(len(request.variables) <= 6 for request in plan.requests)
    assert plan.described == sum(len(request.variables) for request in plan.requests)


def test_a_larger_limit_bundles_scopes_into_fewer_requests(shop):
    small = plan_for(shop, max_variables=4)
    large = plan_for(shop, max_variables=40, max_chars=100_000)

    assert len(large.requests) < len(small.requests)
    assert len(large.requests) == 1
    assert len(large.requests[0].scopes) > 1


def test_modules_come_before_classes_before_functions(shop):
    plan = plan_for(shop, max_variables=100, max_chars=100_000)

    labels = [scope.label.split(" ")[0] for scope in plan.requests[0].scopes]

    assert labels == sorted(labels, key=["module", "class", "method", "function"].index)
    assert labels[0] == "module"


def test_the_request_shows_the_code_and_the_usages(shop):
    plan = plan_for(shop, max_variables=100, max_chars=100_000)

    text = plan.requests[0].messages[1]["content"]

    assert "def add(self, item, qty=1):" in text
    assert "class Cart(Base):" in text
    assert "shop/cart.py:" in text
    assert "e.g. shop/cart.py" in text
    assert "from shop.config import TAX" in text


def test_usages_in_other_files_are_shown_first(shop):
    plan = plan_for(shop, max_variables=100, max_chars=100_000)
    text = plan.requests[0].messages[1]["content"]

    tax = text[text.index("`TAX`") :].split("\n\n")[0]

    assert "e.g. shop/cart.py" in tax


def test_variables_belong_to_numbers_in_order(shop):
    plan = plan_for(shop, max_variables=100, max_chars=100_000)
    request = plan.requests[0]

    text = request.messages[1]["content"]

    for number, variable in enumerate(request.variables, start=1):
        assert f"\n{number}. `{variable.name}`" in text


def test_only_the_selected_kinds_are_planned(shop):
    plan = plan_for(shop, kinds=frozenset({VariableKind.MODULE}))

    assert plan.described == 4
    reasons = {item.error for item in plan.skipped}
    assert reasons == {"skipped: its kind was not selected"}


def test_a_request_budget_leaves_the_rest_skipped(shop):
    plan = plan_for(shop, max_variables=4, max_requests=2)

    assert len(plan.requests) == 2
    assert any("request budget" in item.error for item in plan.skipped)
    assert {item.status for item in plan.skipped} == {SKIPPED}


def test_long_source_is_cut(write_project):
    body = "\n".join(f"    value_{n} = {n}" for n in range(400))
    root = write_project({"big.py": f"def big():\n{body}\n"})
    index = build_index(root)
    resolver = Resolver(index)
    table = build_variable_table(index, resolver)

    plan = plan_annotations(
        resolver,
        table,
        AnnotationOptions(source_limit=300, max_variables=1000, max_chars=10**6),
    )

    text = plan.requests[0].messages[1]["content"]
    assert "# ... truncated" in text
    assert "value_399" not in text.split("Variables:")[0]


def test_the_plan_can_be_costed_before_it_runs(shop):
    plan = plan_for(shop)

    summary = plan.summary()

    assert summary["requests"] == len(plan.requests)
    assert summary["variables"] == plan.described
    assert summary["estimated_prompt_tokens"] > 0
    assert summary["static"] == 4


def test_a_run_describes_every_planned_variable(shop, annotator):
    plan = plan_for(shop)
    client = annotator()

    result = run_annotations(plan, client, workers=1)

    assert client.calls == len(plan.requests)
    assert len(result.annotations) == len(shop[1])
    described = [
        item for item in result.annotations.values() if item.model == "annotator-1"
    ]
    assert len(described) == plan.described
    assert {item.status for item in described} == {OK}
    assert described[0].role == "state"


def test_usage_counts_calls_and_tokens(shop, annotator):
    plan = plan_for(shop)

    result = run_annotations(plan, annotator(), workers=1)

    assert result.usage.requests == len(plan.requests)
    assert result.usage.prompt_tokens == 100 * len(plan.requests)
    assert result.usage.completion_tokens == 40 * len(plan.requests)
    assert (result.usage.cached, result.usage.failed) == (0, 0)
    assert not result.stopped


def test_a_concurrent_run_gives_the_same_answers(shop, annotator):
    plan = plan_for(shop, max_variables=5)

    serial = run_annotations(plan, annotator(), workers=1)
    parallel = run_annotations(plan, annotator(delay=0.01), workers=4)

    assert serial.annotations == parallel.annotations


def test_a_second_run_is_served_from_the_cache(shop, annotator):
    plan = plan_for(shop)
    cache = AnnotationCache()
    first = annotator()
    run_annotations(plan, first, cache=cache, workers=1)

    second = annotator()
    result = run_annotations(plan, second, cache=cache, workers=1)

    assert second.calls == 0
    assert result.usage.cached == len(plan.requests)
    assert result.usage.requests == 0
    assert all(
        item.status is OK
        for item in result.annotations.values()
        if item.model == "annotator-1"
    )


def test_the_cache_survives_in_a_file(shop, annotator, tmp_path):
    plan = plan_for(shop)
    path = tmp_path / "cache" / "annotations.jsonl"
    run_annotations(plan, annotator(), cache=AnnotationCache(path), workers=1)

    reloaded = AnnotationCache(path)
    again = annotator()
    run_annotations(plan, again, cache=reloaded, workers=1)

    assert len(reloaded) == len(plan.requests)
    assert again.calls == 0


def test_a_damaged_cache_file_is_ignored(tmp_path):
    path = tmp_path / "annotations.jsonl"
    path.write_text('not json\n{"key": "k", "entry": {"answers": {}}}\n{"wrong": 1}\n')

    cache = AnnotationCache(path)

    assert len(cache) == 1
    assert cache.get("k") == {"answers": {}}


def test_the_cache_key_depends_on_the_model_and_the_prompt():
    messages = [{"role": "user", "content": "hello"}]

    assert cache_key("a", messages) == cache_key("a", list(messages))
    assert cache_key("a", messages) != cache_key("b", messages)
    assert cache_key("a", messages) != cache_key(
        "a", [{"role": "user", "content": "bye"}]
    )


def test_an_unusable_reply_is_retried_once(shop, annotator):
    plan = plan_for(shop, max_variables=100, max_chars=100_000)
    client = annotator(garbled=1)

    result = run_annotations(plan, client, workers=1)

    assert client.calls == 2
    retry = client.requests[1]
    assert [item["role"] for item in retry[-2:]] == ["assistant", "user"]
    assert "not usable" in retry[-1]["content"]
    assert result.usage.requests == 2
    assert result.usage.prompt_tokens == 200


def test_a_reply_that_stays_unusable_fails_the_request(shop, annotator):
    plan = plan_for(shop, max_variables=100, max_chars=100_000)

    result = run_annotations(plan, annotator(garbled=2), workers=1)

    failed = [item for item in result.annotations.values() if item.status is FAILED]
    assert len(failed) == plan.described
    assert "not a JSON object" in failed[0].error
    assert result.usage.failed == 1


def test_a_variable_the_model_skips_is_marked_missing(shop, annotator):
    plan = plan_for(shop)

    result = run_annotations(plan, annotator(omit={"total"}), workers=1)

    assert result.annotations["shop.cart.Cart.total"].status is MISSING
    assert result.annotations["shop.cart.Cart.items"].status is OK


def test_provider_errors_fail_only_their_requests(shop, annotator):
    plan = plan_for(shop, max_variables=100, max_chars=100_000)

    result = run_annotations(
        plan, annotator(fail=ProviderError("503: overloaded")), workers=1
    )

    failed = [item for item in result.annotations.values() if item.status is FAILED]
    assert {item.error for item in failed} == {"503: overloaded"}
    assert result.usage.failed == 1


def test_repeated_provider_errors_stop_the_run(shop, annotator):
    plan = plan_for(shop, max_variables=2)
    client = annotator(fail=ProviderError("401: invalid api key"))

    result = run_annotations(plan, client, workers=1, failure_limit=3)

    assert len(plan.requests) > 3
    assert client.calls == 3
    assert result.stopped
    skipped = [item for item in result.annotations.values() if item.status is SKIPPED]
    assert any("provider errors in a row" in item.error for item in skipped)


def test_a_run_can_be_stopped(shop, annotator):
    plan = plan_for(shop, max_variables=2)
    client = annotator()

    result = run_annotations(
        plan, client, workers=1, should_stop=lambda: client.calls >= 2
    )

    assert client.calls == 2
    assert result.stopped
    assert any(
        item.error == "the run was stopped" for item in result.annotations.values()
    )


def test_progress_is_reported_per_request(shop, annotator):
    plan = plan_for(shop, max_variables=5)
    seen = []

    run_annotations(
        plan,
        annotator(),
        workers=1,
        progress=lambda done, total: seen.append((done, total)),
    )

    total = len(plan.requests)
    assert seen == [(done, total) for done in range(1, total + 1)]


def test_an_empty_plan_needs_no_model(write_project, annotator):
    root = write_project({"empty.py": "def f():\n    pass\n"})
    index = build_index(root)
    resolver = Resolver(index)
    plan = plan_annotations(resolver, build_variable_table(index, resolver))
    client = annotator()

    result = run_annotations(plan, client)

    assert client.calls == 0
    assert result.annotations == {}
