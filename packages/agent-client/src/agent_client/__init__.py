"""Light client of the agent service, for the apps that use it.

It owns what an app and the service must agree on without the provider core:
today the tool contract an app declares. It depends on pydantic only, so an app
installs it without the provider machinery.
"""
