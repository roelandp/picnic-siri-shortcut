"""Entrypoint `picnic`."""

import argparse
import logging
import sys

from .config import load_settings, setup_logging

log = logging.getLogger("picnic_service")


def cmd_login(args, settings) -> int:
    from .client import PicnicClient

    client = PicnicClient(settings, interactive=True)
    client.login()
    user = client.call("get_user")
    print(f"Ingelogd. Account heeft {user.total_deliveries} bezorgingen. Token staat in {settings.token_file}.")
    return 0


def cmd_explore(args, settings) -> int:
    from .client import PicnicClient
    from .explore import Explorer, StopExploration, format_report

    client = PicnicClient(settings, interactive=sys.stdin.isatty())
    explorer = Explorer(
        client,
        settings,
        wait=not args.no_wait,
        max_articles=args.max_articles,
        skip_articles=args.skip_articles,
        skip_candidates=args.skip_candidates,
        verify_slot=args.verify_slot,
    )
    try:
        summary = explorer.run()
    except StopExploration as exc:
        print(f"Gestopt: {exc}. Draai later opnieuw; gecachte bestellingen en artikelen worden overgeslagen.")
        return 3
    print(format_report(summary))
    print(f"\nSchema en samenvatting staan in {settings.explore_dir}")
    return 0


def cmd_sync(args, settings) -> int:
    from .client import PicnicClient
    from .notify import Notifier
    from .store import Store
    from .sync import run_sync

    store = Store(settings.db_path)
    try:
        client = PicnicClient(settings, interactive=sys.stdin.isatty())
        result = run_sync(client, store, settings, Notifier(settings, store))
        counts = store.counts()
    finally:
        store.close()
    print(f"Sync: {result.status}. {result.message or ''}")
    print(
        f"Database: {counts['deliveries']} bezorgingen, {counts['order_lines']} regels, "
        f"{counts['articles']} artikelen, {counts['delivery_issues']} ontbrekende artikelen."
    )
    return {"ok": 0, "login_required": 2}.get(result.status, 1)


def cmd_serve(args, settings) -> int:
    import dataclasses

    import uvicorn

    from .api import create_app

    # Een spraakopdracht doet een paar calls; daarvoor geldt het API-tempo
    api_settings = dataclasses.replace(settings, min_interval=settings.api_min_interval)
    app = create_app(api_settings)
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_level=settings.log_level.lower())
    return 0


def cmd_choose(args, settings) -> int:
    """Laat zien wat /add zou kiezen, zonder iets aan het mandje toe te voegen."""
    from .choose import decide
    from .client import PicnicClient
    from .shop import Shop
    from .store import Store

    store = Store(settings.db_path)
    try:
        if not args.offline:
            Shop(PicnicClient(settings, interactive=sys.stdin.isatty()), store).refresh_favorites()
        decision = decide(args.term, args.lang, store)
        print(f"Term: {decision.term!r}  ->  {decision.kind} ({decision.reason})")
        if decision.article:
            print(f"  kiest: {decision.article.label}  [{decision.article.article_id}]")
        for option in decision.options:
            print(f"  optie: {option.label}  [{option.article_id}]  {option.recent_count}x afgelopen jaar")
    finally:
        store.close()
    return 0


def cmd_alias(args, settings) -> int:
    from .choose import normalize
    from .store import Store

    store = Store(settings.db_path)
    try:
        if args.delete:
            print("Verwijderd." if store.delete_alias(normalize(args.term)) else "Stond er niet in.")
        elif args.term and args.article_id:
            known = store.article(args.article_id)
            store.set_alias(normalize(args.term), args.article_id, known["name"] if known else None,
                            known["unit_quantity"] if known else None)
            print(f"{normalize(args.term)!r} -> {known['name'] if known else args.article_id}")
        else:
            for row in store.list_aliases():
                print(f"{row['term']:24} {row['article_id']:10} {row['name'] or ''} {row['unit_quantity'] or ''}")
    finally:
        store.close()
    return 0


def cmd_notify_test(args, settings) -> int:
    from .notify import Notifier

    ok = Notifier(settings).send("Testmelding van picnic-service.")
    print("Verstuurd." if ok else "Niet verstuurd, zie log.")
    return 0 if ok else 1


def cmd_schema(args, settings) -> int:
    import json
    from datetime import date

    from .explore import write_schema

    summary_file = settings.explore_dir / f"summary-{date.today().isoformat()}.json"
    summary = json.loads(summary_file.read_text()) if summary_file.exists() else None
    print(write_schema(settings, summary))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="picnic")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="interactief inloggen (met 2FA) en token cachen")

    p = sub.add_parser("explore", help="fase 0: alle endpoints dumpen en samenvatten")
    p.add_argument("--no-wait", action="store_true", help="stoppen in plaats van wachten als de uurcap bereikt is")
    p.add_argument("--max-articles", type=int, default=None, help="maximaal zoveel artikeldetails ophalen")
    p.add_argument("--skip-articles", action="store_true", help="geen artikeldetail ophalen")
    p.add_argument("--skip-candidates", action="store_true", help="geen kandidaat-paden proberen")
    p.add_argument(
        "--verify-slot",
        action="store_true",
        help="set_delivery_slot testen: ander slot zetten, controleren, direct terugzetten",
    )

    sub.add_parser("schema", help="schema-rapport opnieuw maken uit data/raw, zonder requests")
    sub.add_parser("sync", help="fase 1: bestelhistorie bijwerken in data/picnic.db")
    sub.add_parser("notify-test", help="Telegram-testmelding versturen")
    sub.add_parser("serve", help="fase 2: API voor de Shortcuts starten")

    p = sub.add_parser("choose", help="laat zien wat /add bij een term zou kiezen, zonder toe te voegen")
    p.add_argument("term")
    p.add_argument("--lang", choices=["nl", "en"], default="nl")
    p.add_argument("--offline", action="store_true", help="Favorieten niet verversen, geen requests")

    p = sub.add_parser("alias", help="woordenboek tonen, of term aan artikel koppelen")
    p.add_argument("term", nargs="?")
    p.add_argument("article_id", nargs="?")
    p.add_argument("--delete", action="store_true", help="term uit het woordenboek halen")

    args = parser.parse_args(argv)
    settings = load_settings()
    setup_logging(settings.log_level)

    from .client import LoginRequired

    handlers = {
        "login": cmd_login,
        "explore": cmd_explore,
        "schema": cmd_schema,
        "sync": cmd_sync,
        "notify-test": cmd_notify_test,
        "serve": cmd_serve,
        "choose": cmd_choose,
        "alias": cmd_alias,
    }
    try:
        return handlers[args.command](args, settings)
    except LoginRequired as exc:
        log.error("login nodig: %s", exc)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
