"""The eager global model factory is retired.

Production model calls are configured at service startup and executed with
bounded HTTP requests by service.reports. No model client is created on import.
"""
