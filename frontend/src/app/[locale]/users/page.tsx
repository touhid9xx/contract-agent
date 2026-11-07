"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { FieldLabel, Input } from "@/components/ui/input";
import { ApiError, users } from "@/lib/api";
import type { UserRole, UserSummary } from "@/lib/api";
import { authorized } from "@/lib/session";

/** Offered in this order, which is also least-to-most privileged. */
const ROLES: UserRole[] = ["viewer", "operator", "admin"];

/**
 * The server's floor, duplicated as a *hint only*.
 *
 * `app.core.security.MIN_PASSWORD_LENGTH` is the authority and it is not on the
 * wire, so a client that hard-blocked below 12 would be wrong the day that
 * constant moved. This value only picks the wording of the label; the submit
 * button stays enabled and the server refuses a short password with a **422**,
 * because the bound is a pydantic `Field(min_length=...)` on `CreateUserRequest`
 * and `main.py` maps a `RequestValidationError` onto the app's 422 envelope.
 */
const MIN_PASSWORD_LENGTH = 12;

/**
 * Wire value → message key (relative to the `users` namespace).
 *
 * A record rather than `t(\`users.roles.${value}\`)`: `satisfies
 * Record<UserRole, string>` turns a new member of `app.db.enums.UserRole` into a
 * type error here instead of a missing key at run time.
 */
const ROLE_KEYS = {
  viewer: "roles.viewer",
  operator: "roles.operator",
  admin: "roles.admin",
} as const satisfies Record<UserRole, string>;

/**
 * The users screen — the client for four routes that only curl had reached.
 *
 * `GET /auth/users`, `POST /auth/users`, `PATCH /auth/users/{id}` and
 * `POST /auth/users/{id}/password-reset` were written with the account-lifecycle
 * slice and tested through the real ASGI app, but nothing outside a test called
 * them: provisioning a colleague, promoting them, switching an account off or
 * issuing a temporary password were all hand-written requests. Meanwhile the
 * routes were *load-bearing* — `login` fails for a tenant that has no user, and
 * only `create-admin` or this route can produce the first one.
 *
 * Six choices are deliberate:
 *
 * * **Admin only, and the page says so.** All four routes are `require_roles(ADMIN)`.
 *   The query is not even fired for anyone else (`enabled`), because a 403 an
 *   operator can do nothing about is noise, not information.
 * * **The caller's own row has no controls.** The server refuses a self change
 *   with 409 on both mutating routes. Hiding the buttons is not the enforcement —
 *   the route still decides — it just stops the screen offering an action whose
 *   only outcome is a refusal.
 * * **A role change and a deactivation are one action, not two.** Both end the
 *   target's sessions, because the role travels inside their access token. The
 *   screen says that once, above the list, instead of pretending the change is
 *   invisible.
 * * **Resetting a password asks first, and shows the value exactly once.** The
 *   request takes no body (the server generates it), and the response is the only
 *   copy that will ever exist — there is no mail transport to resend it. It is
 *   rendered outside the row it came from so a refetch cannot take it away.
 * * **The role chips are the whole control.** Three buttons that show the current
 *   value by being `primary` — the same idiom the status filters use — rather than
 *   a `<select>` this design system has no primitive for.
 * * **`is_active` is shown per row.** A deactivated account can still be listed,
 *   and without the badge the only visible difference between it and a working one
 *   would be that logging in fails.
 */
export default function UsersPage() {
  const t = useTranslations();
  const locale = useLocale();
  const queryClient = useQueryClient();
  const { user, isAuthenticated } = useAuth();

  const isAdmin = user !== null && user.role === "admin";

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<UserRole>("viewer");
  /** Which row is showing its "reset this password?" confirmation. */
  const [confirmingReset, setConfirmingReset] = useState<string | null>(null);

  const list = useQuery({
    queryKey: ["users", "list"],
    queryFn: () => authorized((accessToken) => users.list(accessToken)),
    // Only an admin may call this route; for anyone else the answer is 403 and a
    // retry cannot change it.
    enabled: isAdmin,
    retry: false,
  });

  const create = useMutation({
    mutationFn: (input: { email: string; password: string; role: UserRole }) =>
      authorized((accessToken) => users.create(accessToken, input)),
    onSuccess: async () => {
      setEmail("");
      setPassword("");
      setRole("viewer");
      await queryClient.invalidateQueries({ queryKey: ["users", "list"] });
    },
  });

  const update = useMutation({
    mutationFn: (input: { id: string; role?: UserRole; is_active?: boolean }) =>
      authorized((accessToken) =>
        users.update(accessToken, input.id, {
          // Omitted rather than sent as `undefined`: the route refuses a body with
          // neither field, and JSON drops an `undefined` key silently — so sending
          // the key at all would be a lie about what this call is asking for.
          ...(input.role === undefined ? {} : { role: input.role }),
          ...(input.is_active === undefined ? {} : { is_active: input.is_active }),
        }),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["users", "list"] });
    },
  });

  const reset = useMutation({
    mutationFn: (target: UserSummary) =>
      authorized((accessToken) => users.resetPassword(accessToken, target.id)),
    onSuccess: async () => {
      setConfirmingReset(null);
      await queryClient.invalidateQueries({ queryKey: ["users", "list"] });
    },
  });

  /** A refusal in the operator's own language, falling back to a generic failure. */
  function describeFailure(cause: unknown): string {
    if (cause instanceof ApiError) {
      if (cause.isForbidden) return t("users.forbidden");
      if (cause.isOffline) return t("users.offline");
      // These four routes carry human `detail` strings, not machine codes, so the
      // screen keys on the status alone. Matching the English prose would break
      // the moment a message was reworded, and the two 409s this screen can still
      // provoke (own account, inactive target) are already prevented by the cards
      // that offer the buttons.
      if (cause.status === 409) return t("users.conflict");
      if (cause.status === 404) return t("users.notFound");
      // 422 is the real one: `CreateUserRequest.password` carries
      // `Field(min_length=MIN_PASSWORD_LENGTH)`, and `main.py` answers a
      // `RequestValidationError` with the app's 422 envelope rather than FastAPI's
      // 422 body — so a too-short password or a bad email lands *here*, not on a
      // 400. 400 stays for `_NOTHING_TO_CHANGE`, which this screen cannot provoke
      // (every mutation sends at least one field) but the route still defines.
      if (cause.status === 422 || cause.status === 400) return t("users.invalidInput");
    }
    return t("users.failed");
  }

  const rows = list.data ?? [];

  const busyId = update.isPending ? update.variables.id : undefined;
  const resettingId = reset.isPending ? reset.variables.id : undefined;

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-6 px-6 py-12">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">{t("users.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("users.subtitle")}</p>
      </header>

      {!isAuthenticated || user === null ? (
        <Card>
          <CardContent className="space-y-3">
            <p className="text-sm text-muted-foreground">{t("users.signInRequired")}</p>
            <Link href={`/${locale}/login`}>
              <Button size="sm">{t("users.signIn")}</Button>
            </Link>
          </CardContent>
        </Card>
      ) : !isAdmin ? (
        <Card>
          <CardContent className="space-y-1">
            <p className="text-sm">{t("users.forbidden")}</p>
            <p className="text-xs text-muted-foreground">{t("users.forbiddenHint")}</p>
          </CardContent>
        </Card>
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle>{t("users.createHeading")}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="space-y-1">
                  <FieldLabel htmlFor="new-user-email">{t("users.emailLabel")}</FieldLabel>
                  <Input
                    id="new-user-email"
                    type="email"
                    autoComplete="off"
                    value={email}
                    onChange={(event) => {
                      setEmail(event.target.value);
                    }}
                  />
                </div>
                <div className="space-y-1">
                  <FieldLabel htmlFor="new-user-password">{t("users.passwordLabel")}</FieldLabel>
                  <Input
                    id="new-user-password"
                    type="password"
                    autoComplete="new-password"
                    value={password}
                    onChange={(event) => {
                      setPassword(event.target.value);
                    }}
                  />
                  <p className="text-xs text-muted-foreground">
                    {t("users.passwordHint", { min: MIN_PASSWORD_LENGTH })}
                  </p>
                </div>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-medium text-muted-foreground">
                  {t("users.roleLabel")}
                </span>
                {ROLES.map((candidate) => (
                  <Button
                    key={candidate}
                    size="sm"
                    variant={candidate === role ? "primary" : "outline"}
                    aria-current={candidate === role ? "true" : undefined}
                    onClick={() => {
                      setRole(candidate);
                    }}
                  >
                    {t(`users.${ROLE_KEYS[candidate]}`)}
                  </Button>
                ))}
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <Button
                  size="sm"
                  disabled={create.isPending || email.trim() === "" || password === ""}
                  onClick={() => {
                    create.mutate({ email: email.trim(), password, role });
                  }}
                >
                  {create.isPending ? t("users.creating") : t("users.createAction")}
                </Button>
              </div>

              {create.isSuccess && (
                <p className="rounded-lg bg-muted px-3 py-2 text-xs" role="status">
                  {t("users.created", { email: create.data.email })}
                </p>
              )}

              {create.isError && (
                <p className="rounded-lg bg-danger-muted px-3 py-2 text-xs text-danger" role="alert">
                  {describeFailure(create.error)}
                </p>
              )}
            </CardContent>
          </Card>

          {/*
            The one consequence an admin cannot see from the list: a change here
            takes effect on the target's next request, because their role travels
            inside the token they already hold.
          */}
          <p className="rounded-lg bg-muted px-3 py-2 text-xs text-muted-foreground">
            {t("users.sessionNote")}
          </p>

          {/*
            The reset answer sits at page level: it is the only copy of the value,
            and a row-level block would disappear with the row on the next refetch.
          */}
          {reset.isSuccess && (
            <Card>
              <CardHeader>
                <CardTitle>
                  {t("users.resetSucceeded", { email: reset.data.user.email })}
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-2">
                <p className="rounded-lg bg-muted px-3 py-2 font-mono text-sm break-all">
                  {reset.data.temporary_password}
                </p>
                <p className="text-xs text-muted-foreground">{t("users.resetOnce")}</p>
              </CardContent>
            </Card>
          )}

          {list.isPending && (
            <Card>
              <CardContent>
                <p className="text-sm text-muted-foreground">{t("users.loading")}</p>
              </CardContent>
            </Card>
          )}

          {list.isError && (
            <Card>
              <CardContent className="space-y-3">
                <p className="text-sm text-danger" role="alert">
                  {describeFailure(list.error)}
                </p>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    void list.refetch();
                  }}
                >
                  {t("users.retry")}
                </Button>
              </CardContent>
            </Card>
          )}

          {!list.isPending && !list.isError && rows.length === 0 && (
            <Card>
              <CardContent className="space-y-1">
                <p className="text-sm">{t("users.empty")}</p>
                <p className="text-xs text-muted-foreground">{t("users.emptyHint")}</p>
              </CardContent>
            </Card>
          )}

          {!list.isPending && !list.isError && rows.length > 0 && (
            <p className="text-xs text-muted-foreground">
              {t("users.countLabel", { count: rows.length })}
            </p>
          )}

          {rows.map((row) => (
            <UserCard
              key={row.id}
              row={row}
              isSelf={row.id === user.id}
              busy={busyId === row.id}
              resetting={resettingId === row.id}
              confirmingReset={confirmingReset === row.id}
              // Only the row an operator acted on may show an error: one shared
              // message would blame every card for one refusal.
              error={
                update.isError && update.variables.id === row.id
                  ? describeFailure(update.error)
                  : reset.isError && reset.variables.id === row.id
                    ? describeFailure(reset.error)
                    : null
              }
              onRole={(next) => {
                update.mutate({ id: row.id, role: next });
              }}
              onToggleActive={() => {
                update.mutate({ id: row.id, is_active: !row.is_active });
              }}
              onAskReset={() => {
                setConfirmingReset(row.id);
              }}
              onCancelReset={() => {
                setConfirmingReset(null);
              }}
              onReset={() => {
                reset.mutate(row);
              }}
            />
          ))}
        </>
      )}

      <p className="text-sm">
        <Link className="text-primary underline-offset-4 hover:underline" href={`/${locale}`}>
          {t("users.backToHome")}
        </Link>
      </p>
    </main>
  );
}

interface UserCardProps {
  row: UserSummary;
  /** The caller's own row: the server refuses self changes, so no control is offered. */
  isSelf: boolean;
  /** This row's role/active mutation is in flight. */
  busy: boolean;
  /** This row's password reset is in flight. */
  resetting: boolean;
  confirmingReset: boolean;
  error: string | null;
  onRole: (role: UserRole) => void;
  onToggleActive: () => void;
  onAskReset: () => void;
  onCancelReset: () => void;
  onReset: () => void;
}

/**
 * One account in the organisation.
 *
 * It answers "who is this, what may they do, and are they switched on" and offers
 * the three acts the routes support. Every button is disabled while one of this
 * row's mutations is in flight, because a second click would race the first on the
 * same row.
 */
function UserCard({
  row,
  isSelf,
  busy,
  resetting,
  confirmingReset,
  error,
  onRole,
  onToggleActive,
  onAskReset,
  onCancelReset,
  onReset,
}: UserCardProps) {
  const t = useTranslations();

  /** A deactivated account cannot be reset: the route refuses with 409. */
  const canReset = !isSelf && row.is_active;
  const working = busy || resetting;

  return (
    <Card aria-busy={working}>
      <CardHeader className="flex flex-wrap items-baseline justify-between gap-2">
        <CardTitle className="break-all">{row.email}</CardTitle>
        <span
          className={`rounded-full px-2 py-0.5 text-xs font-medium ${
            row.is_active ? "bg-muted text-muted-foreground" : "bg-danger-muted text-danger"
          }`}
        >
          {row.is_active ? t("users.activeLabel") : t("users.inactiveLabel")}
        </span>
      </CardHeader>

      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-medium text-muted-foreground">{t("users.roleLabel")}</span>
          {ROLES.map((candidate) => (
            <Button
              key={candidate}
              size="sm"
              variant={candidate === row.role ? "primary" : "outline"}
              aria-current={candidate === row.role ? "true" : undefined}
              disabled={isSelf || working}
              onClick={() => {
                onRole(candidate);
              }}
            >
              {t(`users.${ROLE_KEYS[candidate]}`)}
            </Button>
          ))}
        </div>

        {isSelf && <p className="text-xs text-muted-foreground">{t("users.selfNote")}</p>}

        {!isSelf && (
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant={row.is_active ? "danger" : "secondary"}
              disabled={working}
              onClick={onToggleActive}
            >
              {working && busy
                ? t("users.working")
                : row.is_active
                  ? t("users.deactivateAction")
                  : t("users.reactivateAction")}
            </Button>
          </div>
        )}

        {!isSelf && !row.is_active && (
          <p className="text-xs text-muted-foreground">{t("users.resetInactiveHint")}</p>
        )}

        {canReset && !confirmingReset && (
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="outline" disabled={working} onClick={onAskReset}>
              {resetting ? t("users.resetting") : t("users.resetAction")}
            </Button>
          </div>
        )}

        {canReset && confirmingReset && (
          <div className="space-y-1.5">
            <p className="text-xs font-medium">{t("users.resetConfirm")}</p>
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" variant="danger" disabled={working} onClick={onReset}>
                {resetting ? t("users.resetting") : t("users.resetConfirmAction")}
              </Button>
              <Button size="sm" variant="ghost" disabled={working} onClick={onCancelReset}>
                {t("users.cancel")}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">{t("users.resetConfirmHint")}</p>
          </div>
        )}

        {error !== null && (
          <p className="rounded-lg bg-danger-muted px-3 py-2 text-xs text-danger" role="alert">
            {error}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
