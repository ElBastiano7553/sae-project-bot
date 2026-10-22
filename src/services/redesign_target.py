"""Structure cible de /refonte : profil, style d'affichage et permissions voulues.

À partir de ``config/server.toml`` (``ServerLayout``), ce module répond à trois
questions, sans Discord et sans effet de bord :

* quels éléments font partie du profil choisi (projet, communauté…) ;
* sous quel nom les afficher (style : emojis, séparateurs) ;
* quelles permissions poser, cible par cible (@everyone, Samae, rôles).

Les permissions sont exprimées par *cible* : ``@everyone``, ``@bot`` (Samae),
``@admin`` (rôle ADMIN_ROLE_NAME), ``role:<clé>`` (rôle de la cible). Seules ces
cibles sont gérées par la refonte ; les permissions posées pour d'autres rôles
ou membres ne sont jamais modifiées.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.config import AccessSpec, CategorySpec, ChannelSpec, ProfileSpec, RoleSpec, ServerLayout, StyleSpec

# Accès minimal toujours garanti à Samae sur les éléments qu'il gère.
BOT_MINIMUM = ("view_channel", "read_message_history")
DEFAULT_ACCESS = "public"
NEUTRAL = (frozenset(), frozenset())  # aucune permission particulière

# Permissions voulues pour une cible : (autorisées, refusées).
Rule = tuple[frozenset[str], frozenset[str]]


@dataclass(frozen=True)
class Target:
    layout: ServerLayout
    profile: ProfileSpec | None
    style: StyleSpec

    # --- Appartenance au profil -----------------------------------------------------------

    def in_profile(self, spec: CategorySpec | ChannelSpec | RoleSpec) -> bool:
        return not spec.profiles or (self.profile is not None and self.profile.key in spec.profiles)

    @property
    def roles(self) -> list[RoleSpec]:
        """Rôles du profil, du plus haut au plus bas (ordre du fichier)."""
        return [spec for spec in self.layout.roles.values() if self.in_profile(spec)]

    def channels_of(self, category_key: str) -> list[ChannelSpec]:
        return [spec for spec in self.layout.channels.values() if spec.category_key == category_key and self.in_profile(spec)]

    # --- Noms affichés ---------------------------------------------------------------------

    def name(self, spec: CategorySpec | ChannelSpec) -> str:
        return self.style.render(spec)

    # --- Permissions -----------------------------------------------------------------------

    @property
    def manages_permissions(self) -> bool:
        return bool(self.layout.access)

    def access_of(self, spec: CategorySpec | ChannelSpec) -> AccessSpec | None:
        if not self.layout.access:
            return None
        if isinstance(spec, ChannelSpec) and not spec.access:
            return self.access_of(self.layout.categories[spec.category_key])
        return self.layout.access.get(spec.access or DEFAULT_ACCESS)

    def rules(self, spec: CategorySpec | ChannelSpec, available_roles: set[str], admin_role: bool) -> dict[str, Rule]:
        """Permissions voulues par cible. Les cibles absentes du serveur (rôle non créé…) sont ignorées."""
        access = self.access_of(spec)
        if access is None:
            return {}
        rules: dict[str, Rule] = {}
        for rule in access.rules:
            if rule.target == "@admin" and not admin_role:
                continue
            if rule.target.startswith("role:") and rule.target[5:] not in available_roles:
                continue
            rules[rule.target] = (frozenset(rule.allow), frozenset(rule.deny))
        # Un accès restreint pour @everyone doit garder Samae dans le salon ; un accès public n'a pas besoin de surcharge.
        everyone_deny = rules.get("@everyone", NEUTRAL)[1]
        if "@bot" in rules or everyone_deny:
            allow, deny = rules.get("@bot", NEUTRAL)
            rules["@bot"] = (allow | frozenset(BOT_MINIMUM), deny - frozenset(BOT_MINIMUM))
        rules.setdefault("@everyone", NEUTRAL)
        return rules

    def hides_from_everyone(self, spec: CategorySpec | ChannelSpec) -> bool:
        access = self.access_of(spec)
        return access is not None and any(r.target == "@everyone" and "view_channel" in r.deny for r in access.rules)


def build_target(layout: ServerLayout, profile: str = "", style: str = "") -> Target:
    """Cible pour un profil et un style ; vides : ceux de la section [refonte]."""
    profile_key = profile or layout.policy.profile
    chosen_profile = layout.profiles.get(profile_key)
    if layout.profiles and chosen_profile is None:
        raise ValueError(f"profil inconnu « {profile_key} »")
    style_key = style or (chosen_profile.style if chosen_profile else "") or layout.policy.style
    chosen_style = layout.styles.get(style_key)
    if chosen_style is None:
        if layout.styles:
            raise ValueError(f"style inconnu « {style_key} »")
        chosen_style = StyleSpec("sobre", "Sobre")
    return Target(layout, chosen_profile, chosen_style)
