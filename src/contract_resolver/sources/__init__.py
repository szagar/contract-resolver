"""Pluggable sources behind the OptionResolver port.

* ``StaticSource`` — in-memory candidates (testing / backtest seed). No deps.
* ``BrokerSource`` — live TastyTrade REST + DXLink. Needs the ``broker`` extra
  (``contract-resolver[broker]``); import it from ``.broker`` directly.
"""

from .static import StaticSource

__all__ = ["StaticSource"]
