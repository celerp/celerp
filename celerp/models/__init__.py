# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from celerp.models.accounting import UserCompany  # noqa: F401 - ensure tables registered
from celerp.models.auth import SessionRegistry, SystemRuntimeState, UserAuthState  # noqa: F401 - ensure tables registered
from celerp.models.ai import AIBatchJob, AIConversation, AIMessage  # noqa: F401
from celerp.models.connector_config import ConnectorConfig  # noqa: F401 - ensure connector_configs table registered
from celerp.models.connector_source import ConnectorSource  # noqa: F401 - ensure connector_sources table registered
from celerp.models.import_batch import ImportBatch  # noqa: F401 - ensure import_batches table registered
from celerp.models.migration import MigrationEntityMap, MigrationRun  # noqa: F401 - ensure migration tables registered
from celerp.models.notification import Notification, NotificationRead  # noqa: F401
from celerp.models.share import DocShareToken  # noqa: F401 - ensure doc_share_tokens table registered
from celerp.models.sync_run import SyncRun  # noqa: F401 - ensure sync_runs table registered
from celerp.models.supporter import SupporterBadge  # noqa: F401 - ensure supporter_badges table registered
from celerp.models.payment_closure import PaymentClosure, PaymentRecovery, UnmatchedPayment, UnmatchedRefund  # noqa: F401 - ensure payment tables registered
import celerp.services.company_lock  # noqa: F401,E402 - company settings changes need the company lock
