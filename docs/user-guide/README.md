# KiroCrew on AgentCore User Guide

**English** | [简体中文](README.zh-CN.md)

This guide is for first-time users. It follows the order you actually perform the steps: create an account, sign in, start your sandbox, connect your Kiro account, and start the first session. It also explains what every button on the page does. All screenshots come from a real deployment.

> You need: the access address your deployment operator gave you (an `https://` link), an email address the deployment accepts, and a Kiro account (Builder ID or your organization's SSO).

## Contents

1. [Open the page](#1-open-the-page)
2. [Create an account](#2-create-an-account)
3. [Sign in](#3-sign-in)
4. [Start the sandbox](#4-start-the-sandbox)
5. [Sign in to Kiro](#5-sign-in-to-kiro)
6. [Start your first session](#6-start-your-first-session)
7. [Panel controls](#7-panel-controls)
8. [Stopping and signing out](#8-stopping-and-signing-out)
9. [Troubleshooting](#9-troubleshooting)

## 1. Open the page

Open the address in your browser. On the first visit, KiroCrew's own two welcome dialogs appear first:

**Privacy**: explains the anonymous telemetry. Click **Continue** in the lower right.

![First-run privacy dialog](images/01-first-run-privacy.png)

**Customize**: theme and color scheme. Click **Skip all** in the upper right; everything here can be changed later in Settings.

![Customize dialog](images/02-first-run-customize.png)

With the dialogs closed, the floating panel in the upper right is the **sandbox control panel** this project adds. It always floats above the KiroCrew page and can be dragged. Right now its title is **Sign in to KiroCrew**, with the sign-in form below it.

![Sign-in panel](images/03-sign-in-panel.png)

## 2. Create an account

1. Enter your address in **Email** and a password in **Password**. The rule is printed above the form: at least 8 characters, including a lowercase letter and a number. The eye icon shows or hides the password.
2. Click **Create account**.

![Email and password filled in](images/04-register-filled.png)

3. The page says "We emailed a code to …". Check your inbox for a message titled **Your verification code** from `no-reply@verificationemail.com` (usually within a minute; it may carry an [EXTERNAL] prefix).

![Waiting for the verification code](images/05-confirm-code.png)

4. Type the six-digit code into **Code from the email** and click **Confirm email**. **Resend code** sends a new one; **Back to sign in** returns to the form.

![Entering the code](images/06-confirm-code-entered.png)

Confirmation signs you in automatically and the panel title becomes **Sandbox stopped**.

If your address is not allowed, Create account fails immediately; ask the deployment operator to add your address to the allow list.

## 3. Sign in

With an existing account, enter email and password in the same form and click **Sign in** (Enter in the password field also works).

- The session is kept in the browser: closing the tab and coming back does not ask for the password again. Only **Sign out** on the panel ends it.
- Forgot your password: **Forgot password?** → enter your email → **Send code** → enter the emailed code and a new password → **Reset password**.

After signing in, the sandbox starts on its own; you do not need to click anything. If it was stopped, the panel briefly shows `Sandbox stopped` before startup begins.

![Signed in, sandbox stopped](images/07-signed-in-stopped.png)

## 4. Start the sandbox

Opening the page, or signing in, starts a stopped sandbox automatically. (**Start sandbox** is still there for a sandbox in an error state.) The panel shows three steps with elapsed time: Requesting compute → Restoring workspace → Starting KiroCrew.

![Startup progress](images/08-starting-progress.png)

While the workspace is restored the second step shows its progress, for example **Restoring 22,997 files: 54%**, then **Restored 22,997 files. Launching KiroCrew...** once every file is back. The first start takes about a minute. Later starts restore your last checkpoint, so they depend on how much is in your workspace: a small workspace is ready in about 30 seconds, one with 20,000+ files in 3-4 minutes. When ready, the panel collapses into a small **Sandbox** pill in the upper right with a green dot, and KiroCrew's own header switches to **Gateway connected**.

![Sandbox ready, panel collapsed to a pill](images/09-ready-pill.png)

Click the pill to expand the panel again; the title reads **Sandbox ready**.

![Sandbox ready, full panel](images/10-ready-panel.png)

Expand **Sandbox details** to see the microVM uptime, recent state changes, and the **persisted paths**. Only files under these paths survive stop and restart; everything else is ephemeral:

```
/mnt/workspace/home/.kiro
/mnt/workspace/home/.config
/mnt/workspace/home/.local/share/kiro-cli
/mnt/workspace/artifacts
/mnt/workspace/knowledge
/mnt/workspace/memory
/mnt/workspace/projects
/mnt/workspace/user
```

![Sandbox details](images/11-sandbox-details.png)

## 5. Sign in to Kiro

KiroCrew calls models with your own Kiro account. Sign-in happens inside KiroCrew itself, not in the sandbox panel. You do this once; the sign-in is saved with your workspace and survives Stop safely and restarts.

If you send a message before signing in, KiroCrew answers with a **Sign in to Kiro** button that takes you to the right place:

![Prompt to sign in to Kiro](images/15-chat-sign-in-prompt.png)

Or go there directly: **Settings** → **Agent Harness** → **Kiro Agent** (the default, marked **In use**) → **Kiro sign-in**. Pick one option:

- **Continue with AWS Builder ID**, **Google**, or **GitHub**: a personal account.
- **Company SSO**: your organization's IAM Identity Center. Enter the **Company sign-in URL** (like `https://your-org.awsapps.com/start`) and, if needed, the AWS Region, then click **Continue**.

![Kiro sign-in options in Settings](images/12-kiro-sign-in-settings.png)

KiroCrew then shows a link and a code:

![Device authorization code](images/12-kiro-device-code.png)

1. Open the link in your browser (or copy it with the button next to it).
2. Sign in, check that the code on that page matches, and allow it.
3. Back in KiroCrew the section changes to **Signed in** within a few seconds. The code is valid for about 10 minutes; if it expires, start the sign-in again.

![Kiro signed in](images/13-kiro-signed-in.png)

**Sign out** here disconnects only the Kiro account; the sandbox and your KiroCrew web sign-in are unaffected.

## 6. Start your first session

With the panel collapsed you have the full KiroCrew interface. Click **+ New** in the **Sessions** column, or type in the composer at the bottom and press Enter.

![Typing the first message](images/14-first-message-typed.png)

From here on, everything works like a locally run KiroCrew: sessions, Schedule, Artifacts, Knowledge, Terminal, and so on are in the left navigation. Conversations and files live in your own sandbox; other users cannot see them.

## 7. Panel controls

The floating panel shows different buttons depending on state:

| Button | When shown | What it does |
|---|---|---|
| **Sign in / Create account** | Signed out | Sign in to, or register, a KiroCrew account |
| **Forgot password?** | Signed out | Reset the password with an emailed code |
| **Start sandbox** | Sandbox in an error state (a stopped sandbox starts on its own when the page opens) | Request compute and restore the most recent saved workspace |
| **Stop safely** | Sandbox running | Save an encrypted checkpoint first, then release compute. Use this to stop; do not just close the page |
| **Response in progress** | A reply is being generated | Disabled form of Stop safely; stop after the reply finishes |
| **Retry safely / Check connection** | Connection lost | Re-establish the connection without resubmitting a request that was already accepted |
| **Reload** | Signed in | Reload the page and reconnect without stopping the sandbox |
| **Sign out** | Signed in | Sign out of KiroCrew in this browser. The sandbox and its data remain; sign in again to continue |
| **Minimize** | Panel expanded | Collapse to the pill in the upper right; click the pill to expand |
| **Sandbox details** | Signed in | Show uptime, state history, and persisted paths |

Panel titles and what they mean:

| Title | Meaning |
|---|---|
| Sign in to KiroCrew | Signed out |
| Sandbox stopped | Signed in, sandbox not running; it starts again the next time you open the page |
| Starting your sandbox | Starting; watch the three-step progress |
| Sandbox ready | Running; KiroCrew is usable |
| KiroCrew is responding | A reply is being generated |
| Saving and stopping | Writing a checkpoint and stopping |
| Reconnecting | Connection lost; reconnecting automatically |
| Workspace is read-only | Checkpointing failed; existing data is protected, new changes are not persisted for now |
| Sandbox needs attention | An error; read the message under the title. Start sandbox or Reload usually recovers |

## 8. Stopping and signing out

- **Click Stop safely when you are done.** It writes the workspace to an encrypted checkpoint before releasing compute; opening the page again starts the sandbox and restores exactly that state. Stopping takes from about 10 seconds to a minute depending on workspace size.
- Forgetting is not fatal: an idle sandbox is reclaimed after 15 minutes, and a running sandbox checkpoints itself every few minutes, so at most a few minutes of changes are lost. If background work is running, the sandbox stays up until it finishes.
- **Sign out** only ends the sign-in in this browser; it does not stop the sandbox.

## 9. Troubleshooting

**"Email not allowed" on registration**: ask the deployment operator to add your address to the allow list.

**No verification code**: check spam. The subject is "Your verification code", the sender `no-reply@verificationemail.com`; click Resend code.

**Sandbox needs attention**: click **Reload** first; if that does not help, **Start sandbox**. Your data is in the checkpoint and is not lost by restarting.

**The page keeps showing "The runtime request failed"**: a Stop safely whose final save failed can leave the sandbox unable to serve until the platform reclaims it (up to about an hour). Wait and open the page again; the next sign-in restores your sandbox from the last checkpoint, so no saved work is lost.

**A chat says "Not signed in to Kiro"**: click **Sign in to Kiro** in that message, or follow [step 5](#5-sign-in-to-kiro).

**Slow replies or Gateway offline**: the sandbox may still be cold-starting; wait for the panel to show Sandbox ready. If the panel is ready but the page still shows offline, click Reload.
