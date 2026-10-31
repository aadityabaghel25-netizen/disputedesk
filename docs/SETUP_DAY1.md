# Day 1 setup: step by step

Goal for today: make a fake purchase, have the fake buyer complain, and reply to that
complaint through the API. If this works, DisputeDesk is safe to build.

Time needed: about 1.5-2 hours. Do the steps in order.

---

## Step 0: Tools on your laptop

You likely have these already:
- **Python 3.10+**: check with `python3 --version` (on Windows: `python --version`)
- **VS Code** (or any editor)
- **Git**: check with `git --version`

## Step 1: Put the project on your laptop

1. Download the project files Claude sent you and unzip them into a folder, e.g. `~/projects/disputedesk`.
2. Open a terminal in that folder.

## Step 2: PayPal developer account

1. Go to https://developer.paypal.com and click **Log in to Dashboard**.
2. Log in with your normal PayPal account (or sign up if you don't have one).
   - If sign-up is blocked for your country, stop here and tell Claude; we'll ask on the hackathon Discord.

## Step 3: Check your sandbox test accounts

1. In the dashboard, go to **Testing Tools -> Sandbox Accounts**.
2. You should see two accounts created for you:
   - a **Business** account (this is "you", the seller)
   - a **Personal** account (this is the fake buyer)
3. Check their **Country**. We want **US** for both, so everything works in USD.
   - If they are not US: click **Create account**, pick **United States**, create one **Business** and one **Personal** account.
4. For the **Personal (buyer)** account, click the three dots -> **View/Edit account** and note its **email and password**. You'll use them to log in as the buyer.

## Step 4: Create an app (this gives you the API keys)

1. Go to **Apps & Credentials**. Make sure the toggle at the top says **Sandbox** (not Live).
2. Click **Create App**.
   - Name: `DisputeDesk`
   - Type: **Merchant**
   - Sandbox account: choose your **US Business** account
3. Copy the **Client ID** and the **Secret**.

## Step 5: Put the keys in a .env file

In the project folder:

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

Open `.env` and paste:

```
PAYPAL_CLIENT_ID=paste-client-id-here
PAYPAL_CLIENT_SECRET=paste-secret-here
```

Never share this file or put it on GitHub. `.gitignore` already blocks it.

## Step 6: Test the login

```bash
python3 spike/day1_spike.py auth
```

You should see `[OK] Logged in to PayPal Sandbox`.

## Step 7: Make a fake purchase

```bash
python3 spike/day1_spike.py create-order 40.00
```

1. It prints a link. Open it in your browser.
2. Log in with the **sandbox Personal (buyer)** email and password from Step 3.
3. Click **Pay** / **Continue** to approve.
4. Back in the terminal, take the money:

```bash
python3 spike/day1_spike.py capture <ORDER_ID>
```

## Step 8: The buyer complains (this creates a dispute)

1. Open https://www.sandbox.paypal.com and log in as the **buyer** (Personal account).
2. Go to **Activity**, open the $40 payment.
3. Click **Report a problem** -> **I didn't receive my item** and submit.
4. Wait 2-5 minutes, then:

```bash
python3 spike/day1_spike.py disputes
python3 spike/day1_spike.py dispute <DISPUTE_ID>
```

Tip: make **2 or 3 more purchases and disputes** while you're here, with different reasons
("item not received", "significantly not as described"). We'll need them later.

## Step 9: Reply to the dispute (the real test)

```bash
python3 spike/day1_spike.py message <DISPUTE_ID> "Hi, your parcel was delivered on 20 Oct."
python3 spike/day1_spike.py require-evidence <DISPUTE_ID>
python3 spike/day1_spike.py evidence <DISPUTE_ID>
```

On another dispute, try a settlement offer:

```bash
python3 spike/day1_spike.py offer <OTHER_DISPUTE_ID> 15.00
```

## Step 10: Tell Claude the results

Send Claude:
- which commands said `[OK]` and which said `[FAILED]`
- any files in `spike/results/` whose name starts with `ERROR_`

**Decision after Day 1:**
- `evidence` works -> full DisputeDesk plan, go.
- only `message` / `offer` work -> DisputeDesk still works; it sends messages + offers and produces a downloadable evidence pack.
- disputes never appear -> we debug together, or switch to the backup idea.

## Also do today (5 minutes, for later)

- **Anthropic API key** (for the AI part, needed from Day 6): sign up at https://console.anthropic.com, add a small amount of credit (US$5-10 is plenty for this project), create an API key, and put it in `.env` as `ANTHROPIC_API_KEY=...`.
- **GitHub**: make sure you have an account. We'll create the public repo on Day 3.
- **Register for the hackathon** on Devpost if you haven't: https://paypalaihackathon.devpost.com
