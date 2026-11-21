"""The sampan hub: a relay between the machines of one owner (and, later, of the people of one company).

core.py is the whole behaviour, as plain functions over a sqlite file and an injected clock (no sockets, no third-party
packages); auth.py checks tokens; server.py is the thin HTTP layer. Nothing here is imported by sampan itself."""
