from server.app.grpc_services.healthcheck_service import HealthCheckService
from agent.mini_drop_agent.main import _TASK_TYPE_COLLECTOR


def test_extended_collectors_use_task_type_routes_instead_of_perf_fallback():
    assert HealthCheckService._task_type("off_cpu_wait_profile") == 8
    assert HealthCheckService._profiler_type("off_cpu_wait_profile") == 4
    assert _TASK_TYPE_COLLECTOR[8] == "off_cpu_wait_profile"

    assert HealthCheckService._task_type("trace_endpoint_profile") == 2
    assert _TASK_TYPE_COLLECTOR[2] == "trace_endpoint_profile"

    assert HealthCheckService._task_type("baseline_window_profile") == 9
    assert HealthCheckService._profiler_type("baseline_window_profile") == 7
    assert _TASK_TYPE_COLLECTOR[9] == "baseline_window_profile"

    assert HealthCheckService._task_type("log_scan") == 10
    assert _TASK_TYPE_COLLECTOR[10] == "log_scan"

    assert HealthCheckService._task_type("dependency_check") == 11
    assert _TASK_TYPE_COLLECTOR[11] == "dependency_check"

    assert HealthCheckService._task_type("redis_check") == 12
    assert _TASK_TYPE_COLLECTOR[12] == "redis_check"

    assert HealthCheckService._task_type("python_lock_wait_profile") == 20
    assert _TASK_TYPE_COLLECTOR[20] == "python_lock_wait_profile"

    assert HealthCheckService._task_type("python_exception_profile") == 21
    assert _TASK_TYPE_COLLECTOR[21] == "python_exception_profile"

    assert HealthCheckService._task_type("python_queue_profile") == 22
    assert _TASK_TYPE_COLLECTOR[22] == "python_queue_profile"

    assert HealthCheckService._task_type("python_pool_profile") == 23
    assert _TASK_TYPE_COLLECTOR[23] == "python_pool_profile"

    assert HealthCheckService._task_type("python_retry_timeout_profile") == 24
    assert _TASK_TYPE_COLLECTOR[24] == "python_retry_timeout_profile"

    assert HealthCheckService._task_type("python_cache_profile") == 25
    assert _TASK_TYPE_COLLECTOR[25] == "python_cache_profile"

    assert HealthCheckService._task_type("python_input_profile") == 26
    assert _TASK_TYPE_COLLECTOR[26] == "python_input_profile"
