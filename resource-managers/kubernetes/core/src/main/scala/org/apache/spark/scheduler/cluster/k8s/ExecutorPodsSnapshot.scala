/*
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The ASF licenses this file to You under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with
 * the License.  You may obtain a copy of the License at
 *
 *    http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package org.apache.spark.scheduler.cluster.k8s

import java.util.Locale

import io.fabric8.kubernetes.api.model.Pod

import org.apache.spark.deploy.k8s.Constants._
import org.apache.spark.internal.Logging

/**
 * An immutable view of the current executor pods that are running in the cluster.
 */
private[spark] case class ExecutorPodsSnapshot(executorPods: Map[Long, ExecutorPodState]) {

  import ExecutorPodsSnapshot._

  def withUpdate(updatedPod: Pod): ExecutorPodsSnapshot = {
    val newExecutorPods = executorPods ++ toStatesByExecutorId(Seq(updatedPod))
    new ExecutorPodsSnapshot(newExecutorPods)
  }
}

object ExecutorPodsSnapshot extends Logging {

  def apply(executorPods: Seq[Pod]): ExecutorPodsSnapshot = {
    ExecutorPodsSnapshot(toStatesByExecutorId(executorPods))
  }

  def apply(): ExecutorPodsSnapshot = ExecutorPodsSnapshot(Map.empty[Long, ExecutorPodState])

  private def toStatesByExecutorId(executorPods: Seq[Pod]): Map[Long, ExecutorPodState] = {
    executorPods.map { pod =>
      pod.getMetadata.getLabels.get(SPARK_EXECUTOR_ID_LABEL) match {
        case "EXECID" | null =>
          // The pod has been created by something other than Spark and we should process
          // pod the name to get the ID instead of the label.
          val execIdRE = ".+-([0-9]+)".r
          val podName = pod.getMetadata.getName
          podName match {
            case execIdRE(id) =>
              (id.toLong, toState(pod))
            case _ =>
              throw new Exception(s"Failed to parse podname ${podName}")
          }
        case id =>
          // We have a "real" id label
          (id.toLong, toState(pod))
      }
    }.toMap
  }

  private def toState(pod: Pod): ExecutorPodState = {
    if (isDeleted(pod)) {
      logInfo(s"Pod ${pod} is deleted")
      if (pod.getStatus != null && pod.getStatus.getPhase != null) {
        logInfo(s"Delete pod has phase ${pod.getStatus.getPhase}")
      } else {
        logInfo(s"Deleted pod is missing status or phase.")
      }
      PodDeleted(pod)
    } else {
      val phase = pod.getStatus.getPhase.toLowerCase
      phase match {
        case "pending" =>
          PodPending(pod)
        case "running" =>
          PodRunning(pod)
        case "failed" =>
          PodFailed(pod)
        case "terminating" =>
          PodTerminating(pod)
        case "succeeded" =>
          PodSucceeded(pod)
        case _ =>
          logWarning(s"Received unknown phase $phase for executor pod with name" +
            s" ${pod.getMetadata.getName} in namespace ${pod.getMetadata.getNamespace}")
          PodUnknown(pod)
      }
    }
  }

  private def isDeleted(pod: Pod): Boolean = {
    (pod.getMetadata.getDeletionTimestamp != null &&
      (
        pod.getStatus == null ||
        pod.getStatus.getPhase == null ||
          (pod.getStatus.getPhase.toLowerCase != "terminating" &&
           pod.getStatus.getPhase.toLowerCase != "running" &&
           pod.getStatus.getPhase.toLowerCase != "pending"
          )
      ))
  }
}
