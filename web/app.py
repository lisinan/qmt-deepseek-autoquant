# -*- coding: utf-8 -*-
"""
Flask 应用工厂

创建 app，注册路由，绑定共享的 EventEngine 单例。
"""
from __future__ import annotations

import logging
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
            debug: bool = None) -> None:
    app = create_app(engine)
    app.run(host=host or WEB_HOST, port=port or WEB_PORT,
            debug=debug if debug is not None else WEB_DEBUG,
            threaded=True, use_reloader=False)