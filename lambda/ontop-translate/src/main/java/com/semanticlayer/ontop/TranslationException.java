package com.semanticlayer.ontop;

/**
 * Unchecked exception thrown when Ontop fails to build a reformulator or to
 * reformulate a SPARQL query into native SQL. Every Ontop-originated failure
 * (configuration build, mapping/metadata parse, query parse, reformulation) is
 * wrapped in this single type so callers (the Lambda handler in Task 5) have one
 * failure surface to catch.
 */
public class TranslationException extends RuntimeException {

    private static final long serialVersionUID = 1L;

    /**
     * Create a translation failure with a human-readable message.
     *
     * @param message description of what failed.
     */
    public TranslationException(final String message) {
        super(message);
    }

    /**
     * Create a translation failure wrapping the underlying Ontop cause.
     *
     * @param message description of what failed.
     * @param cause   the originating exception (e.g. an Ontop {@code OBDASpecificationException}).
     */
    public TranslationException(final String message, final Throwable cause) {
        super(message, cause);
    }
}
