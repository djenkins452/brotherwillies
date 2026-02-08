import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class AbstractProvider(ABC):
    """Base class for all data providers (schedule, odds, injuries)."""

    sport = None       # e.g. 'cbb', 'cfb', 'golf'
    data_type = None   # e.g. 'schedule', 'odds', 'injuries'

    @abstractmethod
    def fetch(self):
        """Call external API, return raw response data (dict or list)."""

    @abstractmethod
    def normalize(self, raw):
        """Transform raw API response into list of standardized dicts."""

    @abstractmethod
    def persist(self, normalized):
        """Upsert normalized data into Django models. Return stats dict."""

    def run(self):
        """Orchestrate fetch -> normalize -> persist with error handling."""
        label = f"{self.sport}/{self.data_type}"
        logger.info(f"[{label}] Starting ingestion")
        try:
            raw = self.fetch()
            if not raw:
                logger.warning(f"[{label}] No data returned from API")
                return {'status': 'empty', 'created': 0, 'updated': 0}

            normalized = self.normalize(raw)
            logger.info(f"[{label}] Normalized {len(normalized)} records")

            stats = self.persist(normalized)
            logger.info(f"[{label}] Done — {stats}")
            return stats
        except Exception as e:
            logger.error(f"[{label}] Ingestion failed: {e}", exc_info=True)
            raise
