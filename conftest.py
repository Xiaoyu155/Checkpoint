import asyncio
import inspect

# These test files reference external directories that have been pruned
# (vps_gemini4_gateway, examples/pacer_token_exchange). Skip collection
# so they don't block the rest of the test suite.
collect_ignore = [
    "tests/test_browser_binding_api.py",
    "tests/test_gemini4_gateway.py",
    "tests/test_gemini_session_recovery.py",
    "tests/test_token_exchange_security.py",
]


def pytest_pyfunc_call(pyfuncitem):
    testfunction = pyfuncitem.obj
    if inspect.iscoroutinefunction(testfunction):
        funcargs = pyfuncitem.funcargs
        testargs = {arg: funcargs[arg] for arg in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(testfunction(**testargs))
        return True
    return None
