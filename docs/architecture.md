# Architecture: Distributed Backup System (TCP + SDN)

## 1. Objective
Build a reliable, chunked file backup application over TCP sockets, deployed on a Mininet network where a Ryu SDN controller selects network paths and recovers from failures.

## 2. Components
- Client (h1, h2): splits files into chunks, computes SHA-256 per chunk, sends over TCP, retransmits on NACK or timeout.
- Backup servers (h3, h4): multithreaded TCP servers that verify checksums, store chunks, and reply ACK/NACK.
- Ryu controller: discovers topology, computes paths, installs flow rules, reacts to link failures.
- Mininet topology: two clients, two backup servers, five OpenFlow switches with redundant paths.

## 3. Topology
h1 - s1 - s2 - s4 - h3
h2 - s1 - s3 - s5 - h4
(s2/s3 and s4/s5 are cross-linked to give at least two paths between clients and servers)

## 4. Message flow
1. Client asks the controller for a path to the chosen backup server.
2. Controller computes the path and installs flow rules on each switch along it.
3. Client sends INIT, then CHUNK messages over TCP.
4. Server verifies each chunk's checksum and replies ACK or NACK.
5. Client sends DONE, and the server verifies the whole-file hash.

## 5. Expected network behavior
- Flows are installed along the controller-chosen path before the transfer starts.
- (D2) On link failure the controller recomputes the path and reinstalls flows, and the transfer resumes.

## 6. Application protocol
Message types: INIT, CHUNK, ACK, NACK, QUERY_MISSING, DONE.

