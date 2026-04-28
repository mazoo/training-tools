from dataclasses import dataclass

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.athlete import (
    AthleteRole,
    AthleteToken,
    Permission,
    Role,
    RolePermission,
)
from app.services.auth import get_current_athlete_id

ADMIN_ROLE = "admin"
BACKFILL_FROM_UI = "backfill_from_ui"


@dataclass(frozen=True)
class AuthorizationState:
    roles: list[str]
    permissions: list[str]


async def ensure_default_authorization(db: AsyncSession) -> None:
    """
    Ensure the built-in admin role exists and grant it to the only connected
    athlete when the app is still in its single-user bootstrap state.
    """
    admin_role = await _ensure_admin_role_with_permissions(db)
    await _grant_single_existing_athlete_admin_if_needed(db, admin_role.id)
    await db.commit()


async def grant_initial_admin_if_needed(db: AsyncSession, athlete_id: int) -> None:
    """
    The first connected Strava athlete becomes admin. Later athletes are not
    auto-promoted; their roles can be assigned explicitly in the database.
    """
    admin_role = await _ensure_admin_role_with_permissions(db)
    if not await _has_any_athlete_roles(db) and await _is_only_connected_athlete(db, athlete_id):
        await _grant_role_id(db, athlete_id, admin_role.id)
    await db.commit()


async def get_authorization_state(db: AsyncSession, athlete_id: int) -> AuthorizationState:
    roles_result = await db.execute(
        select(Role.name)
        .join(AthleteRole, AthleteRole.role_id == Role.id)
        .where(AthleteRole.athlete_id == athlete_id)
        .order_by(Role.name)
    )
    roles = [row[0] for row in roles_result.all()]

    permissions_result = await db.execute(
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(AthleteRole, AthleteRole.role_id == RolePermission.role_id)
        .where(AthleteRole.athlete_id == athlete_id)
        .distinct()
        .order_by(Permission.code)
    )
    permissions = [row[0] for row in permissions_result.all()]

    return AuthorizationState(roles=roles, permissions=permissions)


async def athlete_has_permission(
    db: AsyncSession,
    athlete_id: int,
    permission_code: str,
) -> bool:
    result = await db.execute(
        select(Permission.id)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(AthleteRole, AthleteRole.role_id == RolePermission.role_id)
        .where(
            AthleteRole.athlete_id == athlete_id,
            Permission.code == permission_code,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


def require_permission(permission_code: str):
    async def _dependency(
        athlete_id: int = Depends(get_current_athlete_id),
        db: AsyncSession = Depends(get_db),
    ) -> int:
        if not await athlete_has_permission(db, athlete_id, permission_code):
            raise HTTPException(status_code=403, detail="Missing permission")
        return athlete_id

    return _dependency


async def _ensure_admin_role_with_permissions(db: AsyncSession) -> Role:
    await db.execute(
        sqlite_insert(Role)
        .values(name=ADMIN_ROLE, label="Admin")
        .on_conflict_do_update(
            index_elements=["name"],
            set_={"label": "Admin"},
        )
    )
    await db.execute(
        sqlite_insert(Permission)
        .values(
            code=BACKFILL_FROM_UI,
            label="Backfill from UI",
            description="Start the starred-segment effort backfill from the browser UI.",
        )
        .on_conflict_do_update(
            index_elements=["code"],
            set_={
                "label": "Backfill from UI",
                "description": "Start the starred-segment effort backfill from the browser UI.",
            },
        )
    )

    role = await _get_role(db, ADMIN_ROLE)
    permission = await _get_permission(db, BACKFILL_FROM_UI)
    await db.execute(
        sqlite_insert(RolePermission)
        .values(role_id=role.id, permission_id=permission.id)
        .on_conflict_do_nothing(index_elements=["role_id", "permission_id"])
    )
    return role


async def _get_role(db: AsyncSession, name: str) -> Role:
    result = await db.execute(select(Role).where(Role.name == name))
    role = result.scalar_one()
    return role


async def _get_permission(db: AsyncSession, code: str) -> Permission:
    result = await db.execute(select(Permission).where(Permission.code == code))
    permission = result.scalar_one()
    return permission


async def _has_any_athlete_roles(db: AsyncSession) -> bool:
    result = await db.execute(select(AthleteRole.athlete_id).limit(1))
    return result.scalar_one_or_none() is not None


async def _grant_single_existing_athlete_admin_if_needed(
    db: AsyncSession,
    admin_role_id: int,
) -> None:
    if await _has_any_athlete_roles(db):
        return

    result = await db.execute(
        select(AthleteToken.athlete_id)
        .order_by(AthleteToken.athlete_id)
        .limit(2)
    )
    athlete_ids = [row[0] for row in result.all()]
    if len(athlete_ids) == 1:
        await _grant_role_id(db, athlete_ids[0], admin_role_id)


async def _is_only_connected_athlete(db: AsyncSession, athlete_id: int) -> bool:
    result = await db.execute(
        select(AthleteToken.athlete_id)
        .order_by(AthleteToken.athlete_id)
        .limit(2)
    )
    athlete_ids = [row[0] for row in result.all()]
    return athlete_ids == [athlete_id]


async def _grant_role_id(db: AsyncSession, athlete_id: int, role_id: int) -> None:
    await db.execute(
        sqlite_insert(AthleteRole)
        .values(athlete_id=athlete_id, role_id=role_id)
        .on_conflict_do_nothing(index_elements=["athlete_id", "role_id"])
    )
