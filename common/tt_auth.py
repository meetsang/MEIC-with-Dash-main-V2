"""TastyTrade OAuth2 session helpers."""
from __future__ import annotations

import logging
from typing import Any, Optional, Union

from common import tt_config

log = logging.getLogger(__name__)


def create_tastytrade_session(paper: Optional[bool] = None) -> Any:
    """
    Create a TastyTrade Session or PaperSession.

    paper=True  -> tastyware PaperSession (requires TASTYWARE_API_KEY)
    paper=False -> live OAuth Session (client_secret + refresh_token)
    """
    use_paper = tt_config.PAPER_MODE if paper is None else paper

    if use_paper:
        if not tt_config.TASTYWARE_API_KEY:
            raise ValueError(
                'PAPER_MODE is enabled but TASTYWARE_API_KEY is not set in .env'
            )
        from tastytrade.paper import PaperSession

        log.info('Creating Tastyware PaperSession')
        return PaperSession(api_key=tt_config.TASTYWARE_API_KEY)

    if not tt_config.TT_CLIENT_SECRET or not tt_config.TT_REFRESH_TOKEN:
        raise ValueError(
            'TT_CLIENT_SECRET and TT_REFRESH_TOKEN must be set in .env. '
            'Register OAuth at my.tastytrade.com > Manage > My Profile > API'
        )

    from tastytrade import Session

    log.info('Creating TastyTrade OAuth Session (is_test=%s)', tt_config.TT_IS_TEST)
    return Session(
        tt_config.TT_CLIENT_SECRET,
        tt_config.TT_REFRESH_TOKEN,
        is_test=tt_config.TT_IS_TEST,
    )


def bootstrap_account(session: Any, account_number: Optional[str] = None) -> Any:
    """Validate session and resolve account in one event loop."""
    import asyncio

    from tastytrade import Account

    acct_num = account_number or tt_config.TT_ACCOUNT_NUMBER
    if not acct_num:
        raise ValueError('TT_ACCOUNT_NUMBER must be set in .env')

    async def _bootstrap() -> Any:
        await session.validate()
        if hasattr(session, 'api_key'):
            accounts = await Account.a_get(session)
            for a in accounts:
                if str(a.account_number) == str(acct_num):
                    return a
            if accounts:
                return accounts[0]
            raise ValueError('No paper accounts found')
        return await Account.get(session, acct_num)

    return asyncio.run(_bootstrap())


def get_account(session: Any, account_number: Optional[str] = None) -> Any:
    """Resolve trading account from session (validate separately via bootstrap_account)."""
    import asyncio

    from tastytrade import Account

    acct_num = account_number or tt_config.TT_ACCOUNT_NUMBER
    if not acct_num:
        raise ValueError('TT_ACCOUNT_NUMBER must be set in .env')

    if hasattr(session, 'api_key'):
        async def _get():
            accounts = await Account.a_get(session)
            for a in accounts:
                if str(a.account_number) == str(acct_num):
                    return a
            if accounts:
                return accounts[0]
            raise ValueError('No paper accounts found')

        return asyncio.run(_get())

    return asyncio.run(Account.get(session, acct_num))
