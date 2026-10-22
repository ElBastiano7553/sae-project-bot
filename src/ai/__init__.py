"""IA générative (lot 3) : fournisseurs interchangeables et désactivables.

Rien dans ce paquet ne dépend de discord.py. ``build_provider`` choisit le
fournisseur d'après ``AI_PROVIDER`` ; sans configuration valide, l'IA est
simplement désactivée et le reste du bot fonctionne normalement.
"""

from src.ai.base import AIError, AIProvider, AIRefused, AIResponse, AIUnavailable
from src.ai.factory import build_provider

__all__ = ["AIError", "AIProvider", "AIRefused", "AIResponse", "AIUnavailable", "build_provider"]
