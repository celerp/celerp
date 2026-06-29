# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from celerp.models.accounting import UserCompany  # noqa: F401 - ensure tables registered
from celerp.models.auth import SessionRegistry, SystemRuntimeState, UserAuthState  # noqa: F401 - ensure tables registered
from celerp.models.ai import AIBatchJob, AIConversation, AIMessage  # noqa: F401
from celerp.models.connector_config import ConnectorConfig, OutboundQueue  # noqa: F401 - ensure connector_configs + outbound_queue tables registered
from celerp.models.import_batch import ImportBatch  # noqa: F401 - ensure import_batches table registered
from celerp.models.notification import Notification  # noqa: F401
from celerp.models.share import DocShareToken  # noqa: F401 - ensure doc_share_tokens table registered
from celerp.models.sync_run import SyncRun  # noqa: F401 - ensure sync_runs table registered
from celerp.models.supporter import SupporterBadge  # noqa: F401 - ensure supporter_badges table registered
