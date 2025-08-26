import os
continue
return None


# Poll until the icon becomes available (spinner disappears)
btn = None
for _ in range(120): # ~120 * 500ms = 60s
btn = await find_download_button()
if btn:
break
await page.wait_for_timeout(500)
if not btn:
raise RuntimeError("Report did not become downloadable in time. Increase timeout or refine selectors.")


async with page.expect_download() as dl_info:
await btn.click()
download = await dl_info.value
suggested = download.suggested_filename or DEFAULT_FILENAME
file_path = DOWNLOAD_DIR / suggested
await download.save_as(file_path)
print(f"Downloaded: {file_path}")


await context.close()
await browser.close()


if ENABLE_EMAIL:
email_files([file_path])




def email_files(paths):
if not TO_EMAIL:
print("[WARN] ENABLE_EMAIL is true but TO_EMAIL is empty; skipping email.")
return
msg = MIMEMultipart()
msg["From"] = FROM_EMAIL
msg["To"] = TO_EMAIL
msg["Subject"] = f"Statement of Capital Flows – {YESTERDAY.isoformat()}"


body = f"Attached is the Statement of Capital Flows for {YESTERDAY.isoformat()}."
msg.attach(MIMEText(body, "plain"))


for p in paths:
with open(p, "rb") as f:
part = MIMEBase("application", "octet-stream")
part.set_payload(f.read())
encoders.encode_base64(part)
part.add_header("Content-Disposition", f"attachment; filename={Path(p).name}")
msg.attach(part)


with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
server.starttls()
server.login(SMTP_USER, SMTP_PASS)
server.send_message(msg)
print("Email sent to:", TO_EMAIL)




if __name__ == "__main__":
asyncio.run(run_automation())
