# **Project Charter: Enterprise Knowledge & Support Copilot**

# **1\. Executive Summary**

The project is a working product for company employees that allows them to ask questions in a chat and receive answers based on a SharePoint document repository. All system responses must include a link to a specific internal source. If the system cannot find an answer with high confidence, the query is automatically escalated as a ticket to a live support specialist. The product also features an administrator dashboard that highlights gaps in the existing documentation.

# **2\. SMART Goals**

| Metric | Target | Description |
| :---- | :---- | :---- |
| **Faithfulness** | \>85% | Generated answers match factual data |
| **Citation Precision** | \>90% | Accurate links to relevant sources |
| **Recall (Escalation)** | \>90% | Redirection of unanswered queries to a specialist |
| **p95 Latency** | 10 s | Maximum time to answer with citations |

# **3\. Scope**

**In Scope:**

* Chat interface for users (Slack/Teams/Web)  
* Knowledge Base Search (RAG)  
* Source citations in responses  
* Escalation mechanism to humans (Fallback to Human)  
* Role-Based Access Control (RBAC) to restrict search results based on user permissions  
* Microsoft Teams bot

**Should/Could:**

* Knowledge Gap Dashboard and popular queries analytics  
* Feedback loop for users to rate responses  
* Automated conflict resolution in documentation

**Out of Scope:**

* Integration with external ticketing systems  
* Multi-tenant support  
* Languages other than English  
* Generating new code or complex step-by-step instructions  
* Complex multi-turn conversational dialogues

# **4\. Product Differentiation (Key Features)**

* **Actionable Fallback:** In the absence of relevant data, the system does not hallucinate; it creates tickets for real people.  
* **Knowledge Gap Detection:** Usage analytics help identify topics that lack context and require a new article based on unanswered queries.  
* **RBAC for Data:** Data is carefully filtered by access rights during preparation so that the language model does not receive unauthorized information.  
* **Conflict Resolution:** If the system detects contradictions between records, it automatically creates a ticket for a human to specify the correct version.

# **5\. Architecture & Team**

| Name | Role | Responsibilities & Contribution to MVP | Tech Stack |
| :---- | :---- | :---- | :---- |
| **Oleh Pona** | Team Lead, Java Core Developer | Team coordination, overall architecture, and service contracts. Co-develops the Java core service (RBAC, ticket workflow, REST API for web UI). Owns cross-service integrations. | Java, Spring Boot, SQL, REST, Message Queue, React |
| **Vitalii Peleshko** | Lead .NET Integration Engineer | Drives core functionality by building the data ingestion gateway and develops the Microsoft Teams bot interface. | C\#, ASP.NET Core, MS Graph API, Bot Framework SDK |
| **Dmytro Patsula** | Backend Developer (Java) | Builds the ticketing system (Fallback to Human), escalation routing, and the primary REST API for frontend integration. | Java, Spring Boot, REST API, Messaging Queues |
| **Maksym Hrechanyi** | Backend Developer (Java) | Develops asynchronous notification pipelines, event-driven messaging, and handles real-time ticket status updates. | Java, Spring Boot, PostgreSQL |
| **Vasyl Khrustiuk** | Backend Developer (Java) | Manages central data storage schemas, structured data extraction, and creates API endpoints for user feedback loops. | Java, Spring Data JPA, Hibernate, PostgreSQL |
| **Andrii Bezpalko** | AI / Backend Engineer (Python) | Implements the foundational RAG service (vector search, embeddings) and integrates LLM generation with strict source citations. | Python, LangChain/LlamaIndex, Vector DBs, LLM APIs |
| **Taras Ivaniuk** | Backend Developer (Python) | Designs and implements Role-Based Access Control (RBAC) to ensure strict filtering of search results based on user permissions. | Python, FastAPI, SQLAlchemy, PostgreSQL |
| **Zakharii Abramchuk** | Data & Analytics Engineer (Python) | Develops nightly knowledge gap analytics scripts, identifying missing information, and builds popular queries pipelines. | Python, Pandas, Data Engineering, SQL |

# **6\. Key Milestones & Timeline (3 Months)**

| Phase | Focus Areas & Key Deliverables |
| :---- | :---- |
| **Month 1** | Environment setup and Teams app registration. Building the basic end-to-end pipeline without the LLM, basic access filtering, and OpenAPI contracts. |
| **Month 2** | Integration of LLM generation with citations. Fully operational RBAC filtering, working escalation system, and response rendering in the Teams bot. |
| **Month 3** | Implementation of the knowledge gap dashboard and feedback collection. Final access vulnerability testing, UI polishing, and preparation for the final demo in week 12\. |

