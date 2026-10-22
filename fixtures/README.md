# Mock SharePoint Corpus (MVP)

## 1. Test Users & Groups

| ID        | Email              | Groups                            |
| --------- | ------------------ | --------------------------------- |
| `andrii`  | andrii@mock.local  | Test_Engineering, Test_Management |
| `oleh`    | oleh@mock.local    | Test_Management                   |
| `taras`   | taras@mock.local   | Test_Engineering                  |
| `vasyl`   | vasyl@mock.local   | Test_HR                           |
| `zaharii` | zaharii@mock.local | _No groups (General access only)_ |

## 2. RBAC Matrix (Allowed / Denied)

| Document Path    | Allowed                              | Denied                    | Notes                                             |
| ---------------- | ------------------------------------ | ------------------------- | ------------------------------------------------- |
| `/General/*`     | All Groups (Eng, HR, Mgmt) + zaharii | None                      | Inherits site permissions                         |
| `/HR/*`          | Test_HR, Test_Management             | Test_Engineering, zaharii | **Broken inheritance** (`breakInheritance: true`) |
| `/Engineering/*` | Test_Engineering, Test_Management    | Test_HR, zaharii          | **Broken inheritance** (`breakInheritance: true`) |

## 3. Dependencies for Ingestion & RBAC Tests

- **Authentication**: The mock server does not validate real M365 Entra ID tokens. Backend services should send `Authorization: Bearer user:<id>` (e.g., `user:andrii`) to simulate a specific user, or `Bearer app` for full ingestion access.
- **Base URL**: Set the Graph API base URL in the `.NET` / `Python` SDK to `http://localhost:8080/v1.0`.

## 4. How to run

Start the mock server locally via Docker Compose:
`docker compose up -d`
