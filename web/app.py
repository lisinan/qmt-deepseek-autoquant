# -*- coding: utf-8 -*-
"""
Flask 应用工厂

创建 app，注册路由，绑定共享的 EventEngine 单例。
"""
from __future__ import annotations

import logging
import threading
from flask import Flask

from config.settings import WEB_DEBUG, WEB_HOST, WEB_PORT
from engine.event_engine import EventEngine

logger = logging.getLogger(__name__)


def create_app(engine: EventEngine = None) -> Flask:
    app = Flask(__name__,
                template_folder="templates",
                static_folder="static")
    app.config["JSON_AS_ASCII"] = False
    app.config["engine"] = engine or EventEngine()

    from web.routes import bp
    app.register_blueprint(bp)
    logger.info("Flask app 创建完成")
    return app


def run_web(engine: EventEngine = None,
            host: str = None, port: int = None,
            debug: bool = None,
            stop_event: "threading.Event | None" = None) -> None:
    app = create_app(engine)
    _host = host or WEB_HOST
    _port = port or WEB_PORT
    _debug = debug if debug is not None else WEB_DEBUG
    if stop_event is None:
        app.run(host=_host, port=_port, debug=_debug,
                threaded=True, use_reloader=False)
        return
    # 需要与引擎生命周期联动（main.py --web）：用 make_server 把 serve_forever
    # 放到后台线程，主线程轮询 stop_event，收到停止信号即优雅 shutdown。
    # 否则引擎循环退出后 Flask 仍占住端口，守护进程误判「引擎在线」永不拉起。
    from werkzeug.serving import make_server
    server = make_server(_host, _port, app, threaded=True)
    t = threading.Thread(target=server.serve_forever, name="web-serve", daemon=True)
    t.start()
    logger.info(" * Running on http://%s:%s (联动引擎生命周期)", _host, _port)
    try:
        while not stop_event.is_set():
            stop_event.wait(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        t.join(timeout=2)